from datetime import date

import pytest

from koukou.sources.einvoice import sync as sync_mod
from koukou.sources.einvoice.client import EInvoiceClient, EInvoiceError
from koukou.sources.einvoice.sync import month_ranges, sync_einvoice
from koukou.store import Store

from .fake_server import FakeServer


@pytest.fixture(autouse=True)
def fixed_months(monkeypatch):
    real = month_ranges
    monkeypatch.setattr(sync_mod, "month_ranges", lambda n: real(n, date(2026, 10, 14)))


def run(store, server, **kw):
    return sync_einvoice(store, "0912345678", "pw", "dev", months=2, delay=0,
                         client=EInvoiceClient(server), **kw)


def test_full_sync_writes_invoices_and_items(tmp_path):
    store, server = Store(tmp_path / "k.sqlite3"), FakeServer()
    result = run(store, server)
    assert result.invoices == 4 and result.per_month == {"2026-09": 0, "2026-10": 4}
    assert store.monthly() == [{"month": "2026-10", "count": 4, "amount": 500.0}]
    assert store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 8
    assert store.merchants("2026-10")[0]["name"] == "全家"


def test_second_sync_skips_known_items(tmp_path):
    store = Store(tmp_path / "k.sqlite3")
    run(store, FakeServer())
    server = FakeServer()
    result = run(store, server)
    assert result.details_fetched == 0 and "detail" not in server.calls


def test_relogin_when_session_expires(tmp_path):
    store, server = Store(tmp_path / "k.sqlite3"), FakeServer(expire_on=3)
    result = run(store, server)
    assert server.logins == 2 and result.relogins == 1
    assert store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 8


def test_wrong_password_reports_server_message(tmp_path):
    store = Store(tmp_path / "k.sqlite3")
    with pytest.raises(EInvoiceError, match="帳號或密碼錯誤"):
        sync_einvoice(store, "0912345678", "nope", "dev", months=1, delay=0, client=EInvoiceClient(FakeServer()))


def test_month_ranges_cross_year():
    assert month_ranges(3, date(2026, 1, 14)) == [
        ("2025-11", "2025/11/01", "2025/11/30"),
        ("2025-12", "2025/12/01", "2025/12/31"),
        ("2026-01", "2026/01/01", "2026/01/14"),
    ]
