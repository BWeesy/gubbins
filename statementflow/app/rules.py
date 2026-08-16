# SPDX-License-Identifier: MIT
"""Classification rules and the learned-rules store.

Rules map a payee/description pattern to one of the four outflow buckets
(brief §3, §4). Two sources, kept deliberately separate (brief §8):

  * ``rules.yaml``          -- hand-authored, never written by the app.
  * ``rules.learned.yaml``  -- written by the review UI's "remember this payee".

Loading merges them with hand rules first, so a hand rule always wins over a
later learned one, and the app only ever *appends* to the learned file -- it can
never clobber hand-authored config. Matching is a case-insensitive substring
test against the normalised description; first match wins.

``known_destinations`` mark your own accounts that appear only as an unpaired
debit leg, so such debits classify as transfers rather than outflows
(brief §2, §4 step 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import NON_ASSIGNABLE

RULES_FILE = "rules.yaml"
LEARNED_RULES_FILE = "rules.learned.yaml"


@dataclass(frozen=True, slots=True)
class Rule:
    match: str  # stored normalised (upper-case); substring-tested
    bucket: str  # a configured category id, or "other"


@dataclass(frozen=True, slots=True)
class KnownDestination:
    match: str  # normalised
    account_id: str


@dataclass(frozen=True, slots=True)
class RuleSet:
    rules: tuple[Rule, ...]
    known_destinations: tuple[KnownDestination, ...]

    def bucket_for(
        self, description_norm: str, allowed: set[str] | None = None
    ) -> str | None:
        """First matching rule's bucket, or None (-> needs review). ``allowed``
        restricts to buckets valid for the transaction's direction (income
        buckets for a credit, outflow buckets for a debit), so an outflow rule
        never fires on a credit and vice versa."""
        for rule in self.rules:
            if rule.match in description_norm and (
                allowed is None or rule.bucket in allowed
            ):
                return rule.bucket
        return None

    def known_destination_for(self, description_norm: str) -> str | None:
        """The own-account id a debit is a transfer to, or None."""
        for dest in self.known_destinations:
            if dest.match in description_norm:
                return dest.account_id
        return None


def _parse_bucket(value: str, valid_buckets: set[str] | None) -> str:
    """Normalise and validate a rule's target bucket. Income/transfer/
    uncategorised are never valid targets; when ``valid_buckets`` is given (the
    configured categories + 'other'), the bucket must be one of them."""
    bucket = str(value).strip().lower()
    if bucket in NON_ASSIGNABLE:
        raise ValueError(f"bucket {value!r} is not an assignable category")
    if valid_buckets is not None and bucket not in valid_buckets:
        raise ValueError(
            f"bucket {value!r} is not a configured category "
            f"({', '.join(sorted(valid_buckets))})"
        )
    return bucket


def _load_rules_file(
    path: Path, valid_buckets: set[str] | None
) -> tuple[list[Rule], list[KnownDestination]]:
    if not path.exists():
        return [], []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = [
        Rule(
            match=str(r["match"]).strip().upper(),
            bucket=_parse_bucket(r["bucket"], valid_buckets),
        )
        for r in data.get("rules", [])
    ]
    dests = [
        KnownDestination(
            match=str(d["match"]).strip().upper(), account_id=str(d["account_id"])
        )
        for d in data.get("known_destinations", [])
    ]
    return rules, dests


def load_ruleset(directory: Path, valid_buckets: set[str] | None = None) -> RuleSet:
    """Load ``rules.yaml`` then append ``rules.learned.yaml``. Hand rules are
    placed first so they take precedence over learned ones. When
    ``valid_buckets`` is given, every rule's bucket is validated against it."""
    hand_rules, hand_dests = _load_rules_file(directory / RULES_FILE, valid_buckets)
    learned_rules, learned_dests = _load_rules_file(
        directory / LEARNED_RULES_FILE, valid_buckets
    )
    return RuleSet(
        rules=tuple(hand_rules + learned_rules),
        known_destinations=tuple(hand_dests + learned_dests),
    )


def remember_payee(directory: Path, *, match: str, bucket: str) -> None:
    """Append a learned rule to ``rules.learned.yaml`` (created if absent).

    Only ever touches the learned file, never hand-authored ``rules.yaml``
    (brief §8). The stored ``match`` is normalised so it lines up with how
    descriptions are compared. The caller is responsible for validating
    ``bucket`` against the configured categories.
    """
    path = directory / LEARNED_RULES_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    data = data or {}
    rules = data.setdefault("rules", [])
    normalised = match.strip().upper()
    # Idempotent: don't append a learned rule that already maps this exact match.
    for existing in rules:
        if str(existing.get("match", "")).strip().upper() == normalised:
            existing["bucket"] = bucket
            break
    else:
        rules.append({"match": normalised, "bucket": bucket})
    header = (
        "# StatementFlow learned rules -- WRITTEN BY THE APP.\n"
        "# Do not hand-edit; hand-authored rules belong in rules.yaml.\n"
        "# Private/gitignored: entries mirror real payees.\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
