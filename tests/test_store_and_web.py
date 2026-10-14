from datetime import date

from fastapi.testclient import TestClient

from koukou.demo import build_demo_db
from koukou.store import Store, header_date, merchant_name
from koukou.web.app import create_app


def test_merchant_name():
    assert merchant_name("全聯實業股份有限公司中壢青埔分公司") == "全聯"
    assert merchant_name("新加坡商蝦皮娛樂電商有限公司台灣分公司") == "蝦皮"
    assert merchant_name("統一超商股份有限公司桃園縣第三０七分公司") == "7-ELEVEN"
    assert merchant_name("某某小吃店") == "某某小吃店"
    assert merchant_name(None) == "（未知商家）"


def test_header_date_converts_roc_year():
    assert header_date({"invDate": {"year": "115", "month": "5", "date": "1"}, "invoiceTime": "21:23:00"}) == (
        "2026-05-01", "21:23:00")
    assert header_date({}) == (None, None)


def test_demo_is_deterministic_and_consistent(tmp_path):
    a = build_demo_db(tmp_path / "a.sqlite3", today=date(2026, 10, 14))
    b = build_demo_db(tmp_path / "b.sqlite3", today=date(2026, 10, 14))
    assert a.monthly() == b.monthly()
    assert len(a.months()) == 8
    # 每張發票的品項加總都要等於發票金額
    bad = a.db.execute(
        "SELECT COUNT(*) FROM invoices i JOIN (SELECT inv_num, SUM(amount) s FROM items GROUP BY 1) t "
        "USING (inv_num) WHERE ABS(i.amount - t.s) > 0.5").fetchone()[0]
    assert bad == 0


def test_save_is_idempotent(tmp_path):
    store = Store(tmp_path / "k.sqlite3")
    header = {"invNum": "AA00000001", "amount": "10", "invDate": {"year": "115", "month": "10", "date": "1"}}
    store.save(header, [{"rowNum": "1", "description": "x", "amount": "10"}])
    store.save(header, None)  # 只更新表頭，不清掉品項
    assert store.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    assert store.has_items("AA00000001")


def test_web_api_on_demo(tmp_path):
    build_demo_db(tmp_path / "d.sqlite3", today=date(2026, 10, 14)).close()
    client = TestClient(create_app(tmp_path / "d.sqlite3", demo=True), base_url="http://127.0.0.1")
    meta = client.get("/api/meta").json()
    assert meta["demo"] and meta["default_month"] == "2026-10"
    summary = client.get("/api/summary", params={"month": "2026-09"}).json()
    invoices = client.get("/api/invoices", params={"month": "2026-09"}).json()
    assert summary["count"] == len(invoices) > 0
    assert abs(summary["amount"] - sum(i["amount"] for i in invoices)) < 1
    items = client.get(f"/api/invoices/{invoices[0]['inv_num']}/items").json()
    assert items and abs(sum(i["amount"] for i in items) - invoices[0]["amount"]) < 1
    assert client.get("/api/invoices", params={"q": "小籠包"}).json()
    assert client.post("/api/sync", headers={"X-KouKou": "1"}).status_code == 400  # 展示模式不連線
    assert "摳摳" in client.get("/").text
