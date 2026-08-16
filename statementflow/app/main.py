# SPDX-License-Identifier: MIT
"""FastAPI application entry point.

Security model (brief §6, §10): there are no passwords. The app binds
``127.0.0.1`` only, and ``tailscale serve`` terminates TLS and injects the
caller's identity as the ``Tailscale-User-Login`` header. That header is trusted
**only** because ``tailscale serve`` is the sole ingress -- nothing else can
reach the localhost port to spoof it. Never bind a non-loopback interface, or
the trust assumption breaks.

The uploader's identity is recorded for audit only; transaction ownership is
derived from the account's configured owners, never the uploader (brief §2.4).

Run it (bind loopback only):

    uv run --locked python -m uvicorn app.main:app --host 127.0.0.1 --port 8770

Routes so far: upload a statement, run classification, and the review queue
(list + assign). The Sankey / flows endpoint arrives in Phase 3.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .classify import classify_stored
from .config import Config, config_dir, load_config
from .db import connect, init_db, review_queue, set_reviewed_category
from .flows import data_date_range, flows_for_range, transactions_for
from .ingest import ingest_statement
from .money import format_pence
from .profiles import load_profile
from .rules import RuleSet, load_ruleset, remember_payee

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="StatementFlow", version=__version__)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# --- settings & dependencies --------------------------------------------

#: Reject uploads larger than this. Bank CSVs are tiny; this only stops an
#: accidental or malicious huge body from being read wholesale into memory.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    config_dir: Path
    # Defence in depth: when True, data endpoints require the Tailscale identity
    # header, which only `tailscale serve` can inject (a browser on localhost or
    # a direct curl cannot). Off for local dev / tests; on in production.
    require_identity: bool = False
    # Where uploaded statements are retained so an import can be re-run after a
    # profile fix (brief §10). Defaults alongside the DB.
    raw_dir: Path | None = None


def get_settings() -> Settings:
    """Resolve runtime settings from the environment. Overridden in tests."""
    db_path = Path(os.environ.get("STATEMENTFLOW_DB", "./data/statementflow.db"))
    require = os.environ.get("STATEMENTFLOW_REQUIRE_IDENTITY", "1") not in ("0", "", "false")
    raw_dir = Path(os.environ.get("STATEMENTFLOW_RAW_DIR", db_path.parent / "raw"))
    return Settings(
        db_path=db_path, config_dir=config_dir(), require_identity=require,
        raw_dir=raw_dir,
    )


def get_conn(settings: Settings = Depends(get_settings)):
    conn = connect(settings.db_path)
    init_db(conn)  # idempotent; makes first run self-provisioning
    try:
        yield conn
    finally:
        conn.close()


def get_config(settings: Settings = Depends(get_settings)) -> Config:
    return load_config(settings.config_dir)


def get_ruleset(
    settings: Settings = Depends(get_settings),
    config: Config = Depends(get_config),
) -> RuleSet:
    # Validate rule buckets against the configured categories (+ OTHER).
    return load_ruleset(settings.config_dir, valid_buckets=config.assignable_buckets())


def tailscale_user(
    tailscale_user_login: str | None = Header(default=None),
) -> str | None:
    """The caller's Tailscale identity, injected by ``tailscale serve``.

    Returns None when the header is absent (local dev hitting the port
    directly). Used for the *audit* trail on uploads -- not for ownership.
    """
    return tailscale_user_login


def require_identity(
    settings: Settings = Depends(get_settings),
    config: Config = Depends(get_config),
    user: str | None = Depends(tailscale_user),
) -> str | None:
    """Guard for data endpoints, in two steps.

    1. When ``require_identity`` is on, a missing identity header is a 403.
       Only ``tailscale serve`` injects it, so a direct hit on the loopback port
       is rejected. (Note this is *not* a CSRF defence: serve stamps the header
       on every request it proxies, including one a malicious site triggered
       cross-origin.)
    2. When any user configures an ``email``, the identity must match one of
       them -- so a tailnet that has other people or shared nodes on it still
       only serves the household. With no emails configured the tailnet remains
       the only boundary, as documented.

    Returns the configured user id when the caller is recognised (so the audit
    trail records a stable id rather than a raw login), else the raw identity.
    """
    if settings.require_identity and not user:
        raise HTTPException(
            status_code=403,
            detail="access is via tailscale serve only (missing identity header)",
        )
    if config.has_login_allowlist():
        known = config.user_for_login(user)
        if known is None:
            # Deliberately does not echo the identity or the allowlist back.
            raise HTTPException(
                status_code=403, detail="this account is not permitted to use this app"
            )
        return known.id
    return user


# --- routes -------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness check: no auth, no side effects."""
    return {"status": "ok", "version": __version__}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/upload")
async def upload(
    account_id: str = Form(...),
    file: UploadFile | None = File(default=None),
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
    ruleset: RuleSet = Depends(get_ruleset),
    settings: Settings = Depends(get_settings),
    uploader: str | None = Depends(require_identity),
) -> dict:
    """Ingest a statement for an account, then re-run classification.

    The account determines both the bank profile (how to parse) and the owner
    (never the uploader). Re-uploading an overlapping export is idempotent.
    """
    if file is None:
        raise HTTPException(status_code=422, detail="no file uploaded")
    account = config.accounts.get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account {account_id!r}")
    try:
        profile = load_profile(account.bank)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"no usable profile for {account.bank!r}: {exc}")
    # Read at most the cap + 1 byte, so an oversized body is rejected without
    # ever being fully loaded into memory.
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="statement file too large")
    try:
        result = ingest_statement(
            conn,
            profile=profile,
            account_id=account.id,
            owner=account.owner,
            uploaded_by=uploader or "local",
            raw=raw,
            raw_dir=settings.raw_dir,
        )
    except (ValueError, KeyError) as exc:
        # A wrong-bank file, a bad column, or a non-CSV upload -- surface it
        # rather than 500ing. Nothing was committed (ingest commits at the end).
        raise HTTPException(
            status_code=422,
            detail=f"could not read this as a {account.bank} statement: {exc}",
        )
    updated = classify_stored(
        conn, ruleset=ruleset, outflow_buckets=config.outflow_buckets(),
        income_buckets=config.income_buckets(),
    )
    return {
        "import_id": result.import_id,
        "parsed": result.parsed,
        "inserted": result.inserted,
        "duplicates": result.duplicates,
        "classified": updated,
        # Whether the upload was kept for re-import. The path itself is a server
        # detail and is deliberately not returned.
        "retained": result.retained_as is not None,
    }


@app.get("/range")
def get_range(
    conn=Depends(get_conn), _user: str | None = Depends(require_identity)
) -> dict:
    """The earliest/latest transaction dates present, for the date pickers."""
    return data_date_range(conn)


@app.get("/accounts")
def get_accounts(
    config: Config = Depends(get_config), _user: str | None = Depends(require_identity)
) -> dict:
    """Configured accounts, for the upload page's account picker."""
    return {"accounts": [
        {"id": a.id, "label": a.label, "bank": a.bank} for a in config.accounts.values()
    ]}


def _parse_range(start: str, end: str) -> tuple[dt.date, dt.date]:
    try:
        start_date, end_date = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=422, detail="start/end must be YYYY-MM-DD")
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    return start_date, end_date


def _accounts_of_owner(config: Config, owner: str | None) -> set[str] | None:
    """Account ids belonging to ``owner`` (its derived owner value), or None for
    all accounts. Unknown owner -> empty set (an empty, valid result)."""
    if owner is None:
        return None
    return {aid for aid, acc in config.accounts.items() if acc.owner == owner}


@app.get("/flows")
def get_flows(
    start: str,
    end: str,
    transfers: str = "include",
    owner: str | None = None,
    split_unreviewed: bool = False,
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
    _user: str | None = Depends(require_identity),
) -> dict:
    """Sankey nodes + weighted edges + summary for an inclusive ``start``..``end``
    range. ``transfers`` = include|exclude|only; ``owner`` restricts to that
    owner's accounts (for per-owner lanes); ``split_unreviewed`` keeps
    uncategorised rows as their own node."""
    start_date, end_date = _parse_range(start, end)
    if transfers not in ("include", "exclude", "only"):
        raise HTTPException(status_code=422, detail="transfers must be include|exclude|only")
    return flows_for_range(
        conn, config, start_date, end_date, transfers=transfers,
        account_ids=_accounts_of_owner(config, owner), split_unreviewed=split_unreviewed,
    )


@app.get("/transactions")
def get_transactions(
    start: str,
    end: str,
    category: str,
    owner: str | None = None,
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
    _user: str | None = Depends(require_identity),
) -> dict:
    """The transactions behind a Sankey node (drill-down), for an inclusive
    ``start``..``end`` range, optionally restricted to an owner's accounts."""
    start_date, end_date = _parse_range(start, end)
    items = transactions_for(
        conn, config, start_date, end_date, category=category,
        account_ids=_accounts_of_owner(config, owner),
    )
    return {"category": category, "items": items}


@app.post("/classify")
def run_classify(
    conn=Depends(get_conn),
    ruleset: RuleSet = Depends(get_ruleset),
    config: Config = Depends(get_config),
    _user: str | None = Depends(require_identity),
) -> dict:
    return {
        "classified": classify_stored(
            conn, ruleset=ruleset, outflow_buckets=config.outflow_buckets(),
            income_buckets=config.income_buckets(),
        )
    }


@app.get("/review")
def get_review(
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
    _user: str | None = Depends(require_identity),
) -> dict:
    """The queue of transactions still needing a human bucket (brief §8)."""
    items = []
    for row in review_queue(conn):
        account = config.accounts.get(row["account_id"])
        items.append(
            {
                "id": row["id"],
                "date": row["date"],
                "account": account.label if account else row["account_id"],
                "amount": format_pence(row["amount_signed"]),
                # Direction decides which bucket set applies: a credit gets income
                # buckets, a debit outflow buckets.
                "kind": "income" if row["amount_signed"] > 0 else "outflow",
                "description": row["description_raw"],
                # The token "remember" learns on by default; the UI shows it as an
                # editable suggestion so the user controls exactly what is learned.
                "payee": row["payee_norm"],
                "category_hint": row["category_hint"],
            }
        )
    return {
        "outflow_buckets": sorted(config.outflow_buckets()),
        "income_buckets": sorted(config.income_buckets()),
        "items": items,
    }


class ReviewDecision(BaseModel):
    bucket: str
    remember: bool = False
    # Optional override of the string to learn. Defaults to the row's payee token
    # (never the full description, which would only re-match the identical row).
    match: str | None = None


@app.post("/review/{txn_id}")
def post_review(
    txn_id: int,
    decision: ReviewDecision,
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
    settings: Settings = Depends(get_settings),
    _user: str | None = Depends(require_identity),
) -> dict:
    """Assign a bucket to one transaction; optionally learn the payee.

    "Remember" appends a rule to ``rules.learned.yaml`` (never hand-authored
    ``rules.yaml``), keyed on the payee token (or an explicit ``match``), so
    future imports of the *same payee* -- not just the identical row --
    auto-classify.
    """
    bucket = decision.bucket.strip().lower()

    row = conn.execute(
        "SELECT payee_norm, description_norm, amount_signed FROM transactions "
        "WHERE id = ?",
        (txn_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown transaction")

    # A credit may only get an income bucket; a debit only an outflow bucket.
    allowed = (
        config.income_buckets() if row["amount_signed"] > 0
        else config.outflow_buckets()
    )
    if bucket not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"bucket {decision.bucket!r} is not valid for this transaction",
        )

    set_reviewed_category(conn, txn_id=txn_id, category=bucket)
    conn.commit()

    learned = None
    if decision.remember:
        match = (decision.match or "").strip() or row["payee_norm"] or row["description_norm"]
        if match:
            remember_payee(settings.config_dir, match=match, bucket=bucket)
            learned = match.strip().upper()
    return {"id": txn_id, "bucket": bucket, "remembered": learned}
