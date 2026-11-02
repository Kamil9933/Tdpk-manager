"""The data layer against a fake Supabase client. Guards the 2026-11-01 bug
where `await _run(q).data` (attribute taken from the coroutine, not the
result) made list reads silently return [] behind their try/except."""
import ast
import asyncio
import os
import types

import pytest

from conftest import ROOT
from core import supabase_client as sbc

ROWS = [{"id": 1, "status": "draft"}, {"id": 2, "status": "draft"}]


class Query:
    """Chainable stand-in for a supabase-py query builder."""
    def __getattr__(self, name):
        if name == "execute":
            return lambda: types.SimpleNamespace(data=list(ROWS), count=len(ROWS))
        return lambda *a, **k: self


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    monkeypatch.setattr(sbc, "get_client", lambda: types.SimpleNamespace(table=lambda name: Query()))


def test_no_attribute_access_on_unawaited_run():
    """`await _run(q).data` parses as `await (_run(q).data)` -- always wrong."""
    tree = ast.parse(open(os.path.join(ROOT, "core", "supabase_client.py"), encoding="utf-8").read())
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Await) and isinstance(n.value, ast.Attribute)
           and isinstance(n.value.value, ast.Call) and getattr(n.value.value.func, "id", "") == "_run"]
    assert not bad, f"await applied after attribute access on lines {bad}"


@pytest.mark.parametrize("call", [
    lambda d: d.list_vendor_purchases(),
    lambda d: d.list_expenses("2026-10-01", "2026-10-31"),
    lambda d: d.list_filament_purchases(),
    lambda d: d.list_postex_payouts(),
    lambda d: d.list_employee_payments(None, "2026-10-01", "2026-10-31"),
    lambda d: d.list_queue(),
    lambda d: d.list_queue(statuses=["queued"], printer="bambu1"),
    lambda d: d.list_reminders(status="pending"),
    lambda d: d.list_seo_suggestions(status="draft"),
    lambda d: d.list_vendors(),
    lambda d: d.list_agent_messages("c"),
    lambda d: d.list_filament_consumption(days=60),
    lambda d: d.list_due_reminders(),
])
def test_list_reads_return_rows(call):
    assert asyncio.run(call(sbc.Database())) == ROWS


def test_single_reads_and_counts():
    d = sbc.Database()
    assert asyncio.run(d.get_seo_suggestion(1)) == ROWS[0]
    assert asyncio.run(d.latest_seo_run()) == ROWS[0]
    assert asyncio.run(d.count_seo_suggestions()) == 2
    assert asyncio.run(d.order_has_filament_consumption(5)) is True
