# SPDX-License-Identifier: MIT
"""SQLite storage: schema and connection helpers.

Only two tables are persisted (brief §6); users, accounts, bank profiles and
rules are config, loaded from YAML, not DB rows. Money is stored as INTEGER
minor units (pence) -- never a float -- so sums are exact.

The DB file lives under the state dir and is gitignored; it drops straight into
the existing Restic backup set (brief §6, §11).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Bump when the persisted shape changes; a fresh DB is stamped with this. The
#: schema is ABI once real history sits behind it -- migrate, do not silently
#: reinterpret (same discipline as glowbridge's state schema).
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS imports (
    id                  INTEGER PRIMARY KEY,
    uploaded_by_user_id TEXT    NOT NULL,   -- Tailscale identity, audit only
    uploaded_at         TEXT    NOT NULL,   -- ISO-8601 UTC
    bank_profile        TEXT    NOT NULL,
    source_hash         TEXT    NOT NULL,   -- hash of the raw upload, for idempotency
    row_count           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY,
    import_id         INTEGER NOT NULL REFERENCES imports(id),
    account_id        TEXT    NOT NULL,
    owner             TEXT    NOT NULL,     -- a user id or 'joint', from the account
    date              TEXT    NOT NULL,     -- ISO-8601 date
    tx_time           TEXT,                 -- HH:MM:SS when the bank supplies it, else NULL
    seq               INTEGER NOT NULL,     -- same-day ordering (brief §5a)
    amount_signed     INTEGER NOT NULL,     -- minor units (pence); credit +, debit -
    balance_after     INTEGER,              -- minor units, when the profile maps it
    description_raw   TEXT    NOT NULL,
    description_norm  TEXT    NOT NULL,
    payee_norm        TEXT    NOT NULL,     -- merchant token; what "remember payee" learns on
    dedup_hash        TEXT    NOT NULL,     -- hash(account_id, stable source id)
    category          TEXT    NOT NULL,
    category_hint     TEXT,                 -- the bank's own category, a rules hint (brief §7)
    counter_account_id TEXT,                -- set on both legs of a paired transfer
    reviewed          INTEGER NOT NULL DEFAULT 0
);

-- Overlapping re-uploads are idempotent: the same statement line hashes the
-- same, so a unique dedup_hash makes re-import a no-op (brief §6).
CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_dedup
    ON transactions(dedup_hash);

CREATE INDEX IF NOT EXISTS ix_transactions_account_date
    ON transactions(account_id, date, seq);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) a SQLite connection with sane
    pragmas. Foreign keys are off by default in SQLite; turn them on."""
    path = Path(db_path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI runs sync dependencies and the request
    # handler on different threadpool threads, so a per-request connection is
    # legitimately touched from more than one thread (never concurrently). The
    # connection is not shared across requests, so this is safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if absent and stamp the schema version. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def record_import(
    conn: sqlite3.Connection,
    *,
    uploaded_by_user_id: str,
    uploaded_at: str,
    bank_profile: str,
    source_hash: str,
    row_count: int,
) -> int:
    """Insert an audit row for one upload and return its id. `uploaded_by` is
    the Tailscale identity that performed the upload -- audit only, never used
    to derive transaction ownership (brief §2.4)."""
    cur = conn.execute(
        "INSERT INTO imports(uploaded_by_user_id, uploaded_at, bank_profile, "
        "source_hash, row_count) VALUES (?, ?, ?, ?, ?)",
        (uploaded_by_user_id, uploaded_at, bank_profile, source_hash, row_count),
    )
    return int(cur.lastrowid)


def insert_transaction_if_new(
    conn: sqlite3.Connection,
    *,
    import_id: int,
    account_id: str,
    owner: str,
    date: str,
    tx_time: str | None,
    seq: int,
    amount_signed: int,
    balance_after: int | None,
    description_raw: str,
    description_norm: str,
    payee_norm: str,
    dedup_hash: str,
    category: str,
    category_hint: str | None,
) -> bool:
    """Insert one transaction, or skip it if its `dedup_hash` already exists.

    Returns True if a row was inserted, False if it was a duplicate. The unique
    index on `dedup_hash` is what makes overlapping re-uploads idempotent
    (brief §6); `INSERT OR IGNORE` turns the conflict into a silent skip whose
    outcome we read from `rowcount`.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO transactions("
        "import_id, account_id, owner, date, tx_time, seq, amount_signed, "
        "balance_after, description_raw, description_norm, payee_norm, dedup_hash, "
        "category, category_hint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            import_id, account_id, owner, date, tx_time, seq, amount_signed,
            balance_after, description_raw, description_norm, payee_norm, dedup_hash,
            category, category_hint,
        ),
    )
    return cur.rowcount == 1


def apply_classification(
    conn: sqlite3.Connection,
    *,
    txn_id: int,
    category: str,
    counter_account_id: str | None,
) -> None:
    """Write an engine decision onto a transaction. Callers must not pass rows a
    human has already reviewed -- the engine never overwrites a manual verdict."""
    conn.execute(
        "UPDATE transactions SET category = ?, counter_account_id = ? WHERE id = ?",
        (category, counter_account_id, txn_id),
    )


def reviewed_ids(conn: sqlite3.Connection) -> set[int]:
    """Ids of transactions a human has reviewed -- excluded from auto-reclassify."""
    return {
        row["id"]
        for row in conn.execute("SELECT id FROM transactions WHERE reviewed = 1")
    }


def review_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Transactions awaiting a human bucket: still UNCATEGORISED (brief §8)."""
    return conn.execute(
        "SELECT id, account_id, date, seq, amount_signed, description_raw, "
        "description_norm, payee_norm, category_hint FROM transactions "
        "WHERE category = 'uncategorised' ORDER BY date, seq"
    ).fetchall()


def set_reviewed_category(
    conn: sqlite3.Connection, *, txn_id: int, category: str
) -> None:
    """Record a human's bucket for a transaction and mark it reviewed."""
    conn.execute(
        "UPDATE transactions SET category = ?, reviewed = 1 WHERE id = ?",
        (category, txn_id),
    )
