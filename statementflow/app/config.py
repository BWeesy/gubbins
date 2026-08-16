# SPDX-License-Identifier: MIT
"""Config loading: users and accounts from ``APP_CONFIG_DIR``.

These are config, not DB rows (brief §6). Real config is private and gitignored;
only ``*.example.yaml`` templates are committed. The loader reads from
``APP_CONFIG_DIR`` (dev default ``./config``; under systemd
``/var/lib/statementflow/config``). Config is user intent -- the app never
writes it back (the one exception, app-learned rules, lives in ``rules.py``).

Classification rules load separately (``rules.py``) so the app-written learned
rules never sit next to hand-authored config.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import JOINT, Category

CONFIG_DIR_ENV = "APP_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("./config")

#: Reserved ids a configured category may not use -- they are structural.
_RESERVED_CATEGORY_IDS = frozenset(c.value for c in Category)

#: Used when no categories.yaml is present, to preserve the original buckets.
_DEFAULT_CATEGORIES = (("savings", "Savings"), ("bills", "Bills"),
                       ("investment", "Investment"))


def config_dir() -> Path:
    """Resolve the active config directory from ``APP_CONFIG_DIR``."""
    return Path(os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR))


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    # The identity `tailscale serve` reports for this person (the login of their
    # tailnet account, e.g. the Google address they signed in with). When any
    # user sets one, the app only serves callers whose identity matches -- see
    # ``Config.user_for_login``.
    email: str | None = None


@dataclass(frozen=True, slots=True)
class Account:
    """An in-ecosystem current/joint account node (brief §2.1)."""

    id: str
    label: str
    bank: str  # which profile parses this account's exports
    owners: tuple[str, ...]
    opening_balance: int | None = None  # minor units (pence), balance anchor
    as_of: dt.date | None = None

    @property
    def owner(self) -> str:
        """Ownership derived from ``owners`` (brief §2.4): one owner -> that
        user; two or more -> the literal ``joint``. Never the uploader."""
        return self.owners[0] if len(self.owners) == 1 else JOINT


@dataclass(frozen=True, slots=True)
class CategoryDef:
    """A user-configured bucket. ``kind`` is 'outflow' (spending: mortgage,
    energy, ...) or 'income' (salary, refund, ...)."""

    id: str
    label: str
    kind: str = "outflow"


def _default_categories() -> dict[str, CategoryDef]:
    return {cid: CategoryDef(cid, label) for cid, label in _DEFAULT_CATEGORIES}


@dataclass(frozen=True, slots=True)
class Config:
    users: dict[str, User]
    accounts: dict[str, Account]
    # Configurable buckets, both kinds. Defaults preserve the original savings/
    # bills/investment (outflow) when no categories.yaml is present.
    categories: dict[str, CategoryDef] = field(default_factory=_default_categories)

    def has_login_allowlist(self) -> bool:
        """Whether any user declares an email, i.e. whether the allowlist is in
        force. With none configured the tailnet itself remains the only boundary
        (the documented baseline), so this stays opt-in rather than locking out a
        config that predates the field."""
        return any(u.email for u in self.users.values())

    def user_for_login(self, login: str | None) -> User | None:
        """The configured user for an identity reported by ``tailscale serve``,
        or None if it matches nobody. Compared case-insensitively: email
        capitalisation is not meaningful and providers vary."""
        if not login:
            return None
        wanted = login.strip().lower()
        for user in self.users.values():
            if user.email and user.email.strip().lower() == wanted:
                return user
        return None

    def _ids_of_kind(self, kind: str) -> set[str]:
        return {c.id for c in self.categories.values() if c.kind == kind}

    def outflow_buckets(self) -> set[str]:
        """Buckets an outflow (debit) may be assigned: configured outflow
        categories plus the static OTHER catch-all."""
        return self._ids_of_kind("outflow") | {Category.OTHER.value}

    def income_buckets(self) -> set[str]:
        """Buckets an income (credit) may be assigned: configured income
        categories plus the static generic INCOME."""
        return self._ids_of_kind("income") | {Category.INCOME.value}

    def assignable_buckets(self) -> set[str]:
        """Every assignable bucket (both kinds). The direction-specific sets
        (income_buckets / outflow_buckets) enforce that a credit only gets an
        income bucket and a debit only an outflow bucket."""
        return self.outflow_buckets() | self.income_buckets()


def _load_yaml(path: Path) -> dict:
    """Read a config file, treating an absent one as empty.

    A fresh install is exactly this case: the service unit creates the config
    directory and the admin populates it afterwards, so the app must come up and
    answer rather than 500 on every request until then. Note the consequence:
    with no ``users.yaml`` there is no email allowlist, so the tailnet is the
    only boundary (see ``Config.has_login_allowlist``).
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a YAML mapping")
    return data


def _load_categories(directory: Path) -> dict[str, CategoryDef]:
    """Load the configurable outflow buckets from ``categories.yaml``. Absent ->
    the default savings/bills/investment. A configured id may not shadow a
    structural category (income/transfer/other/uncategorised)."""
    path = directory / "categories.yaml"
    if not path.exists():
        return _default_categories()
    categories: dict[str, CategoryDef] = {}
    for entry in _load_yaml(path).get("categories", []):
        cid = str(entry["id"]).strip().lower()
        if cid in _RESERVED_CATEGORY_IDS:
            raise ValueError(f"category id {cid!r} is reserved (structural)")
        if cid in categories:
            raise ValueError(f"duplicate category id: {cid!r}")
        kind = str(entry.get("kind", "outflow")).strip().lower()
        if kind not in ("income", "outflow"):
            raise ValueError(f"category {cid!r}: kind must be income or outflow")
        categories[cid] = CategoryDef(cid, str(entry.get("label", cid)), kind)
    if not categories:
        raise ValueError("categories.yaml defines no categories")
    return categories


def load_config(directory: Path | None = None) -> Config:
    """Load and validate users and accounts.

    Cross-checks that every account owner is a known user and that a joint
    account's owners are distinct -- a dangling owner id is a config error we
    surface loudly rather than silently attributing to nobody.
    """
    directory = directory or config_dir()

    users_raw = _load_yaml(directory / "users.yaml").get("users", [])
    users: dict[str, User] = {}
    seen_emails: set[str] = set()
    for entry in users_raw:
        email = entry.get("email")
        email = str(email).strip() if email else None
        if email:
            key = email.lower()
            # Two users sharing a login would make ownership ambiguous, and is
            # far more likely a copy-paste slip than an intent.
            if key in seen_emails:
                raise ValueError(f"duplicate user email: {email!r}")
            seen_emails.add(key)
        user = User(id=str(entry["id"]), name=str(entry["name"]), email=email)
        if user.id in users:
            raise ValueError(f"duplicate user id: {user.id!r}")
        users[user.id] = user

    accounts_raw = _load_yaml(directory / "accounts.yaml").get("accounts", [])
    accounts: dict[str, Account] = {}
    for entry in accounts_raw:
        owners = tuple(str(o) for o in entry["owners"])
        if not owners:
            raise ValueError(f"account {entry.get('id')!r} has no owners")
        for owner_id in owners:
            if owner_id not in users:
                raise ValueError(
                    f"account {entry['id']!r} owner {owner_id!r} is not a known user"
                )
        as_of = entry.get("as_of")
        if as_of is not None and not isinstance(as_of, dt.date):
            raise ValueError(f"account {entry['id']!r}: as_of must be a date")
        account = Account(
            id=str(entry["id"]),
            label=str(entry["label"]),
            bank=str(entry["bank"]),
            owners=owners,
            opening_balance=entry.get("opening_balance"),
            as_of=as_of,
        )
        if account.id in accounts:
            raise ValueError(f"duplicate account id: {account.id!r}")
        accounts[account.id] = account

    return Config(
        users=users, accounts=accounts, categories=_load_categories(directory)
    )
