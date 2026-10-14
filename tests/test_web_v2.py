"""本機網頁：帳號連結、安全防護、線上展示匯出。"""

import json
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from koukou import credentials
from koukou.sources.einvoice import client as client_mod
from koukou.static_site import build
from koukou.web.app import create_app

H = {"X-KouKou": "1"}


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """測試不碰真的鑰匙圈，也不讀環境變數裡的帳密。"""
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.delenv("EINVOICE_PHONE", raising=False)
    monkeypatch.delenv("EINVOICE_PASSWORD", raising=False)
    credentials._session.clear()
    yield
    credentials._session.clear()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "k.sqlite3"), base_url="http://127.0.0.1")


def test_rejects_other_hosts(tmp_path):
    c = TestClient(create_app(tmp_path / "k.sqlite3"), base_url="http://evil.example")
    assert c.get("/api/meta").status_code == 403


def test_state_changes_need_custom_header(client):
    assert client.post("/api/sync").status_code == 403
    assert client.post("/api/credentials", json={"phone": "0912345678", "password": "x"}).status_code == 403


def test_credentials_flow(client):
    assert client.get("/api/credentials").json() == {"configured": False, "phone": None}
    assert client.post("/api/credentials", headers=H, json={"phone": "12345", "password": "x"}).status_code == 422
    r = client.post("/api/credentials", headers=H, json={"phone": "0912345678", "password": "pw", "remember": True})
    assert r.json() == {"configured": True, "remembered": False}  # 沒有鑰匙圈時只記在記憶體
    assert client.get("/api/credentials").json() == {"configured": True, "phone": "0912…678"}
    assert client.delete("/api/credentials", headers=H).json() == {"configured": False}


def test_sync_without_login_asks_to_connect(client):
    r = client.post("/api/sync", headers=H)
    assert r.status_code == 400 and "還沒連結" in r.json()["detail"]


def test_login_rejection_is_reported_as_login_error(client, monkeypatch):
    def reject(*args, **kwargs):
        raise client_mod.LoginRejected("登入被拒（result=-151，帳號或密碼錯誤）")

    monkeypatch.setattr("koukou.sources.einvoice.sync.sync_einvoice", reject)
    client.post("/api/credentials", headers=H, json={"phone": "0912345678", "password": "bad"})
    assert client.post("/api/sync", headers=H).status_code == 200
    for _ in range(50):
        state = client.get("/api/sync").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["error_kind"] == "login"


def test_static_site_export(tmp_path):
    out = build(tmp_path / "site", today=date(2026, 10, 14))
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '<meta name="koukou-mode" content="static">' in html
    assert 'src="static/app.js"' in html  # 相對路徑，放在 /koukou/ 子路徑也能用
    ledger = json.loads((out / "data" / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["meta"]["static"] and ledger["meta"]["default_month"] == "2026-10"
    for month in ledger["monthly"]:
        m = ledger["months"][month["month"]]
        assert m["summary"]["count"] == len(m["invoices"]) == month["count"]
        assert all(inv["items"] for inv in m["invoices"])
