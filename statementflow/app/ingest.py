# SPDX-License-Identifier: MIT
"""Ingest: parse an export, attach account/owner/seq, and import idempotently.

This ties the pieces together for Phase 1: parse a raw export (``parse.py``)
into rows, derive same-day ordering (``seq``), compute each row's dedup key, and
insert via ``db.insert_transaction_if_new`` so overlapping re-uploads are a
no-op. Classification (transfer pairing + rule bucketing) is a separate Phase 2
step; rows land here as UNCATEGORISED.

Ownership is passed in by the caller (resolved from account config), never taken
from the uploader (brief §2.4).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from .models import Category
from .parse import StatementRow, parse_statement
from .profiles import CsvProfile
from .strategies import dedup_hash

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class IngestResult:
    import_id: int
    parsed: int
    inserted: int
    duplicates: int
    retained_as: str | None = None


def retain_raw(
    raw_dir: Path, account_id: str, raw: bytes, *, suffix: str = ".csv"
) -> Path:
    """Keep the uploaded bytes under ``raw_dir`` so an import can be re-run later
    if a profile or parser improves (brief §10) -- otherwise the only copy is at
    the bank, behind a download limit.

    Named by content hash, so re-uploading the same export is a no-op rather than
    piling up copies; the ``imports`` table maps that hash back to who uploaded
    it and when. The account id is sanitised before use as a path segment: it
    comes from config rather than the request, but a stray separator must never
    escape the directory.
    """
    safe_account = _UNSAFE_PATH_CHARS.sub("_", account_id).lstrip(".") or "unknown"
    digest = hashlib.sha256(raw).hexdigest()
    dest = raw_dir / safe_account / f"{digest}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        # Write via a temp file in the same directory, then rename, so a crash
        # never leaves a half-written statement that looks complete.
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(raw)
        tmp.replace(dest)
    return dest


def _assign_seq(rows: list[StatementRow]) -> list[int]:
    """Deterministic same-day ordering (brief §5a). Within each date, order by
    time then original file order, and number from 0. Rows without a time fall
    back to file order, which the enumerate index preserves."""
    buckets: dict[dt.date, list[tuple[str, int]]] = defaultdict(list)
    for idx, row in enumerate(rows):
        buckets[row.date].append((row.time, idx))
    seq_by_idx: dict[int, int] = {}
    for items in buckets.values():
        for seq, (_time, idx) in enumerate(sorted(items)):
            seq_by_idx[idx] = seq
    return [seq_by_idx[idx] for idx in range(len(rows))]


def ingest_statement(
    conn: Connection,
    *,
    profile: CsvProfile,
    account_id: str,
    owner: str,
    uploaded_by: str,
    raw: bytes,
    now: dt.datetime | None = None,
    raw_dir: Path | None = None,
    source_suffix: str = ".csv",
) -> IngestResult:
    """Parse ``raw`` and import its rows for ``account_id`` (owned by ``owner``).

    Idempotent: importing the same or an overlapping export again inserts only
    the rows not already present.

    When ``raw_dir`` is given the upload is retained there first -- before
    parsing, so a file that fails to parse is still kept: that is exactly the
    case a profile fix would later rescue.
    """
    retained = str(retain_raw(raw_dir, account_id, raw, suffix=source_suffix)) if raw_dir else None

    rows = parse_statement(raw, profile)
    seqs = _assign_seq(rows)
    source_hash = hashlib.sha256(raw).hexdigest()

    from .db import insert_transaction_if_new, record_import

    when = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    import_id = record_import(
        conn,
        uploaded_by_user_id=uploaded_by,
        uploaded_at=when,
        bank_profile=profile.name,
        source_hash=source_hash,
        row_count=len(rows),
    )

    inserted = 0
    for row, seq in zip(rows, seqs):
        new = insert_transaction_if_new(
            conn,
            import_id=import_id,
            account_id=account_id,
            owner=owner,
            date=row.date.isoformat(),
            tx_time=row.time or None,
            seq=seq,
            amount_signed=row.amount_signed,
            balance_after=row.balance_after,  # None for banks without a balance column
            description_raw=row.description_raw,
            description_norm=row.description_norm,
            payee_norm=row.payee_norm,
            dedup_hash=dedup_hash(profile, account_id, row),
            category=Category.UNCATEGORISED.value,
            category_hint=row.category_hint,
        )
        inserted += int(new)
    conn.commit()

    return IngestResult(
        import_id=import_id,
        parsed=len(rows),
        inserted=inserted,
        duplicates=len(rows) - inserted,
        retained_as=retained,
    )
