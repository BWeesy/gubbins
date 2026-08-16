# SPDX-License-Identifier: MIT
"""Phase 2: end-to-end API -- upload, classify, review -- via TestClient.

Uses a temp DB and a temp config dir; the bank profile is the real public
``profiles/monzo.yaml``. The statement is the SYNTHETIC fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.db import connect
from app.main import Settings, app, get_settings

_FIXTURE = Path(__file__).parent / "fixtures" / "monzo_synthetic.csv"


def _make_client(tmp_path, *, require_identity=False):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "users.yaml").write_text(
        yaml.safe_dump({"users": [{"id": "alice", "name": "Alice"}]})
    )
    (config_dir / "accounts.yaml").write_text(
        yaml.safe_dump({"accounts": [
            {"id": "monzo-alice", "label": "Monzo", "bank": "monzo", "owners": ["alice"]},
        ]})
    )
    settings = Settings(db_path=tmp_path / "sf.db", config_dir=config_dir,
                        require_identity=require_identity)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app), settings


@pytest.fixture
def client(tmp_path):
    yield _make_client(tmp_path)
    app.dependency_overrides.clear()


@pytest.fixture
def secure_client(tmp_path):
    yield _make_client(tmp_path, require_identity=True)
    app.dependency_overrides.clear()


def _upload(tc, **headers):
    return tc.post(
        "/upload",
        data={"account_id": "monzo-alice"},
        files={"file": ("monzo.csv", _FIXTURE.read_bytes(), "text/csv")},
        headers=headers,
    )


def test_upload_ingests_and_classifies(client):
    tc, _ = client
    resp = _upload(tc)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["parsed"], body["inserted"], body["duplicates"]) == (6, 6, 0)


def test_upload_reupload_is_idempotent(client):
    tc, _ = client
    _upload(tc)
    again = _upload(tc).json()
    assert (again["inserted"], again["duplicates"]) == (0, 6)


def test_ownership_is_from_account_not_uploader(client):
    tc, settings = client
    # Upload as bob (Tailscale identity) but the account is owned by alice.
    _upload(tc, **{"Tailscale-User-Login": "bob@example.com"})
    conn = connect(settings.db_path)
    owners = {r["owner"] for r in conn.execute("SELECT owner FROM transactions")}
    uploaders = {r["uploaded_by_user_id"] for r in conn.execute(
        "SELECT uploaded_by_user_id FROM imports")}
    assert owners == {"alice"}            # from the account
    assert uploaders == {"bob@example.com"}  # audit only


def test_review_queue_lists_unmatched_rows_both_directions(client):
    tc, _ = client
    _upload(tc)
    review = tc.get("/review").json()
    # Unmatched debits (2 coffees, foreign cafe, -£200 transfer) AND unmatched
    # credits (salary, refund) all need review now.
    assert len(review["items"]) == 6
    assert review["outflow_buckets"] == ["bills", "investment", "other", "savings"]
    assert review["income_buckets"] == ["income"]  # no configured income categories
    kinds = {i["kind"] for i in review["items"]}
    assert kinds == {"income", "outflow"}


def test_assign_bucket_removes_from_queue(client):
    tc, _ = client
    _upload(tc)
    first = tc.get("/review").json()["items"][0]  # earliest row is a coffee (debit)
    resp = tc.post(f"/review/{first['id']}", json={"bucket": "other"})
    assert resp.status_code == 200
    assert len(tc.get("/review").json()["items"]) == 5


def test_income_row_takes_income_buckets_only(client):
    tc, _ = client
    _upload(tc)
    salary = next(i for i in tc.get("/review").json()["items"] if i["kind"] == "income")
    # An outflow bucket is rejected for a credit...
    assert tc.post(f"/review/{salary['id']}", json={"bucket": "savings"}).status_code == 422
    # ...the generic income bucket is accepted.
    assert tc.post(f"/review/{salary['id']}", json={"bucket": "income"}).status_code == 200


def test_remember_learns_payee_token_not_full_description(client):
    """Regression for the review of the review: "remember" must learn the payee
    token (which generalises across locations), not the full description (which
    would only ever re-match the identical row)."""
    tc, settings = client
    _upload(tc)
    coffee = next(
        i for i in tc.get("/review").json()["items"] if "COFFEE" in i["description"].upper()
    )
    assert coffee["payee"] == "COFFEE BAR"  # the Name column, not the noisy desc
    resp = tc.post(f"/review/{coffee['id']}", json={"bucket": "other", "remember": True})
    learned = (settings.config_dir / "rules.learned.yaml").read_text()
    # Learned on the payee token, so it would match a coffee at a new location.
    assert "COFFEE BAR" in learned.upper()
    assert "LONDON" not in learned.upper()  # location noise not baked in
    assert resp.json()["remembered"] == "COFFEE BAR"


def test_remember_accepts_an_explicit_match_override(client):
    tc, settings = client
    _upload(tc)
    item = tc.get("/review").json()["items"][0]
    tc.post(f"/review/{item['id']}",
            json={"bucket": "bills", "remember": True, "match": "my custom token"})
    learned = (settings.config_dir / "rules.learned.yaml").read_text()
    assert "MY CUSTOM TOKEN" in learned.upper()


def test_reject_non_outflow_bucket(client):
    tc, _ = client
    _upload(tc)
    item = tc.get("/review").json()["items"][0]
    assert tc.post(f"/review/{item['id']}", json={"bucket": "income"}).status_code == 422


def test_flows_over_a_date_range(client):
    tc, _ = client
    _upload(tc)
    r = tc.get("/flows?start=2026-05-01&end=2026-05-31")
    assert r.status_code == 200
    body = r.json()
    assert (body["start"], body["end"]) == ("2026-05-01", "2026-05-31")
    assert body["nodes"] and body["edges"]


def test_flows_rejects_bad_range(client):
    tc, _ = client
    assert tc.get("/flows?start=2026-13-01&end=2026-05-31").status_code == 422  # bad date
    assert tc.get("/flows?start=nonsense&end=2026-05-31").status_code == 422
    assert tc.get("/flows?start=2026-06-01&end=2026-05-01").status_code == 422  # start > end
    assert tc.get("/flows?start=2026-05-01").status_code == 422  # missing end


def test_flows_view_params(client):
    tc, _ = client
    _upload(tc)
    base = "/flows?start=2026-05-01&end=2026-05-31"
    assert tc.get(base).json()["summary"]["by_owner"]  # summary present
    assert tc.get(f"{base}&transfers=only").status_code == 200
    assert tc.get(f"{base}&transfers=bogus").status_code == 422
    assert tc.get(f"{base}&split_unreviewed=true").status_code == 200
    assert tc.get(f"{base}&owner=alice").status_code == 200


def test_accounts_endpoint_lists_configured_accounts(client):
    tc, _ = client
    accounts = tc.get("/accounts").json()["accounts"]
    assert any(a["id"] == "monzo-alice" and a["bank"] == "monzo" for a in accounts)


def test_upload_unparseable_file_is_422_not_500(client):
    tc, _ = client
    r = tc.post(
        "/upload",
        data={"account_id": "monzo-alice"},
        files={"file": ("junk.csv", b"not,a,monzo,export\n1,2,3\n", "text/csv")},
    )
    assert r.status_code == 422
    assert "monzo" in r.json()["detail"].lower()


def test_transactions_drilldown(client):
    tc, _ = client
    _upload(tc)
    # The synthetic fixture's two £3.50 coffees are uncategorised debits -> 'other'.
    rows = tc.get("/transactions?start=2026-05-01&end=2026-05-31&category=other").json()
    coffees = [r for r in rows["items"] if "COFFEE" in r["description"].upper()]
    assert len(coffees) == 2


def test_range_endpoint_reports_data_span(client):
    tc, _ = client
    assert tc.get("/range").json() == {"min": None, "max": None}
    _upload(tc)  # synthetic fixture spans 2026-05-01 .. 2026-05-04
    assert tc.get("/range").json() == {"min": "2026-05-01", "max": "2026-05-04"}


def test_upload_too_large_is_rejected(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr("app.main.MAX_UPLOAD_BYTES", 8)  # fixture is far bigger
    assert _upload(tc).status_code == 413


def test_identity_required_when_configured(secure_client):
    tc, _ = secure_client
    # No identity header -> rejected (only tailscale serve injects it).
    assert tc.get("/review").status_code == 403
    assert _upload(tc).status_code == 403
    # With the header -> allowed.
    ok = tc.get("/review", headers={"Tailscale-User-Login": "alice@example.com"})
    assert ok.status_code == 200


def test_healthz_open_without_identity(secure_client):
    tc, _ = secure_client
    assert tc.get("/healthz").status_code == 200
