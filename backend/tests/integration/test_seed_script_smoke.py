"""Smoke test for scripts/seed_demo_data.py (Step 16 Priority 2, item 18).

Deliberately OFFLINE: overrides the root conftest's autouse `clean_db`
(same reasoning as tests/ai/conftest.py, just scoped to this one file
instead of a whole directory - the rest of tests/integration/ still needs
the real DB-wiping fixture) so this file never touches a real Supabase
project. This is also the ONLY place the seed script's dev-project guard
is exercised - per the task, the script itself must never be run against a
real project during this work.

The fake Supabase client below is a generic, schemaless in-memory
Postgrest stand-in (table/select/insert/upsert/update/delete + eq/neq/in_/
like/is_/maybe_single, plus rpc()) - broad enough that the REAL service
functions the seed script calls (accounts_service.open_account,
cards_service.issue_card, face_auth_service, conversations_service,
statements_service) run against it unmodified. That is the point: a smoke
test that only exercised the seed script's own glue code would miss a
regression in the FK order or an accidental raw-SQL insert slipped into
one of those services.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts import seed_demo_data


@pytest.fixture(autouse=True)
def clean_db():
    return None


class _FakeTable:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self._filters: list[tuple[str, str, object]] = []
        self._op: tuple[str, dict | list[dict] | None] | None = None
        self._single = False

    def select(self, *_args: object, **_kwargs: object) -> _FakeTable:
        self._op = self._op or ("select", None)
        return self

    def insert(self, payload: dict | list[dict]) -> _FakeTable:
        self._op = ("insert", payload)
        return self

    def upsert(self, payload: dict | list[dict], **_kwargs: object) -> _FakeTable:
        self._op = ("upsert", payload)
        return self

    def update(self, payload: dict) -> _FakeTable:
        self._op = ("update", payload)
        return self

    def delete(self) -> _FakeTable:
        self._op = ("delete", None)
        return self

    def eq(self, col: str, val: object) -> _FakeTable:
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col: str, val: object) -> _FakeTable:
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col: str, vals: object) -> _FakeTable:
        self._filters.append(("in", col, vals))
        return self

    def like(self, col: str, pattern: object) -> _FakeTable:
        self._filters.append(("like", col, pattern))
        return self

    def is_(self, col: str, val: object) -> _FakeTable:
        self._filters.append(("is", col, val))
        return self

    def order(self, *_args: object, **_kwargs: object) -> _FakeTable:
        return self

    def maybe_single(self) -> _FakeTable:
        self._single = True
        return self

    def _matches(self, row: dict) -> bool:
        for op, col, val in self._filters:
            rv = row.get(col)
            if op == "eq" and str(rv) != str(val):
                return False
            if op == "neq" and str(rv) == str(val):
                return False
            if op == "in":
                assert isinstance(val, (list, tuple, set))
                if str(rv) not in {str(v) for v in val}:
                    return False
            if op == "like":
                pattern = re.escape(str(val)).replace("%", ".*")
                if not re.fullmatch(pattern, str(rv or "")):
                    return False
            if op == "is" and rv != (None if val in ("null", None) else val):
                return False
        return True

    async def execute(self) -> SimpleNamespace:
        kind, payload = self._op or ("select", None)

        if kind == "insert":
            assert payload is not None
            items: list[dict] = payload if isinstance(payload, list) else [payload]
            created = []
            for item in items:
                row = dict(item)
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("created_at", datetime.now(UTC).isoformat())
                self._rows.append(row)
                created.append(row)
            return SimpleNamespace(data=created)

        if kind == "upsert":
            assert payload is not None
            items = payload if isinstance(payload, list) else [payload]
            out = []
            for item in items:
                existing = next(
                    (r for r in self._rows if item.get("id") and r.get("id") == item.get("id")),
                    None,
                )
                if existing is not None:
                    existing.update(item)
                    out.append(existing)
                else:
                    row = dict(item)
                    row.setdefault("id", str(uuid.uuid4()))
                    row.setdefault("created_at", datetime.now(UTC).isoformat())
                    self._rows.append(row)
                    out.append(row)
            return SimpleNamespace(data=out)

        if kind == "update":
            assert isinstance(payload, dict)
            matched = [r for r in self._rows if self._matches(r)]
            for row in matched:
                row.update(payload)
            return SimpleNamespace(data=matched)

        if kind == "delete":
            matched = [r for r in self._rows if self._matches(r)]
            for row in matched:
                self._rows.remove(row)
            return SimpleNamespace(data=matched)

        matched = [r for r in self._rows if self._matches(r)]
        if self._single:
            return SimpleNamespace(data=matched[0] if matched else None)
        return SimpleNamespace(data=matched)


class _FakeRpc:
    async def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=None)


class FakeSupabase:
    """Records every table touched, so the test can assert the write
    sequence never includes anything outside table()/rpc() - i.e. no raw
    SQL, no direct DB connection."""

    def __init__(self) -> None:
        self.store: dict[str, list[dict]] = {}
        self.tables_touched: set[str] = set()
        self.rpc_calls: list[tuple[str, dict]] = []

    def table(self, name: str) -> _FakeTable:
        self.tables_touched.add(name)
        return _FakeTable(self.store.setdefault(name, []))

    def rpc(self, name: str, params: dict) -> _FakeRpc:
        self.rpc_calls.append((name, params))
        return _FakeRpc()


# ---------------------------------------------------------------------------
# Guard - the one thing exercised against a URL string, never a real client.
# ---------------------------------------------------------------------------


def test_guard_refuses_a_non_dev_url_without_confirm():
    with pytest.raises(SystemExit):
        seed_demo_data._guard_dev_project("https://xlyhhnmjpdzsovhzsbvf.supabase.co", False)


def test_guard_allows_the_dev_url():
    seed_demo_data._guard_dev_project(
        f"https://{seed_demo_data.DEV_PROJECT_MARKER}.supabase.co", False
    )  # must not raise


def test_guard_allows_a_non_dev_url_with_explicit_confirm():
    # Must not raise.
    seed_demo_data._guard_dev_project("https://xlyhhnmjpdzsovhzsbvf.supabase.co", True)


# ---------------------------------------------------------------------------
# Full run against the fake client, executed twice to prove idempotence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_uses_only_service_layer_writes(monkeypatch, capsys):
    fake = FakeSupabase()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(seed_demo_data, "get_client", _fake_get_client)

    await seed_demo_data.seed(confirm=True)
    first_stdout = capsys.readouterr().out

    await seed_demo_data.seed(confirm=True)
    second_stdout = capsys.readouterr().out

    summary_line = (
        "Seeded DEMO_USER_A (email: ana@demo.local) and DEMO_USER_B "
        "(email: bogdan@demo.local). Password for both: demo1234."
    )
    assert summary_line in first_stdout
    assert summary_line in second_stdout

    users = fake.store["users"]
    assert {u["email"] for u in users} == {"ana@demo.local", "bogdan@demo.local"}
    assert len(users) == 2  # upsert, not a duplicate insert, across both runs

    user_a_id, user_b_id = str(seed_demo_data.DEMO_USER_A_ID), str(seed_demo_data.DEMO_USER_B_ID)
    accounts_a = [a for a in fake.store["accounts"] if a["user_id"] == user_a_id]
    accounts_b = [a for a in fake.store["accounts"] if a["user_id"] == user_b_id]
    assert len(accounts_a) == 2
    assert len(accounts_b) == 3

    cards = fake.store["cards"]
    assert len(cards) == 2  # one per user, no accumulation on the 2nd run

    journals_a = [
        j for j in fake.store["journal_transactions"]
        if j["idempotency_key"].startswith(f"seed:{seed_demo_data.DEMO_USER_A_ID}:")
    ]
    journals_b = [
        j for j in fake.store["journal_transactions"]
        if j["idempotency_key"].startswith(f"seed:{seed_demo_data.DEMO_USER_B_ID}:")
    ]
    assert len(journals_a) == len(seed_demo_data._user_a_transactions())
    assert len(journals_b) == len(seed_demo_data._user_b_transactions())
    assert len(fake.store["ledger_entries"]) == len(journals_a) + len(journals_b)

    conversations = fake.store["conversations"]
    statements = fake.store["statements"]
    assert len(conversations) == 1  # User B only
    assert len(statements) == 1
    assert statements[0]["row_count"] == 6

    assert fake.store["face_credentials"] == []  # fixture absent - skipped, not faked

    # No table outside the expected service-layer surface was ever touched -
    # in particular nothing resembling a raw-SQL / direct-connection escape
    # hatch, since FakeSupabase only exposes table()/rpc() at all.
    expected_tables = {
        "users", "accounts", "cards", "journal_transactions", "ledger_entries",
        "audit_log", "conversations", "statements", "statement_rows",
        "documents", "transfers", "scheduled_transfers", "face_credentials",
        "face_confirmations", "sessions", "referral_rewards",
    }
    assert fake.tables_touched <= expected_tables

    assert any(name == "grant_opening_balance" for name, _ in fake.rpc_calls)
