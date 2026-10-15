"""自動對獎與固定支出偵測。"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from koukou import lottery as L
from koukou.demo import build_demo_db
from koukou.recurring import detect, summary
from koukou.store import Store
from koukou.web.app import create_app

FIX = Path(__file__).parent / "fixtures"


def page(name):
    return (FIX / f"lottery_{name}.html").read_text(encoding="utf-8")


def test_parse_official_pages():
    """從財政部稅務入口網擷取的真實 HTML：頭獎號碼被拆成兩個 span，期別只寫在 etw-on 的導覽連結裡。"""
    now, last = L.parse_page(page("index")), L.parse_page(page("lastNumber"))
    assert (now.period, now.special, now.grand, now.first) == ("11508", "89996565", "91098182",
                                                               ["54348835", "44991397", "06595111"])
    assert (last.period, last.special, last.grand, last.first) == ("11506", "38548029", "10138845",
                                                                   ["24121106", "28589937", "83663333"])
    with pytest.raises(ValueError):
        L.parse_page("<html>改版了</html>")


@pytest.mark.parametrize("inv, prize", [
    ("AB89996565", ("特別獎", 10_000_000)), ("AB91098182", ("特獎", 2_000_000)),
    ("AB54348835", ("頭獎", 200_000)), ("AB14348835", ("二獎", 40_000)), ("AB10348835", ("三獎", 10_000)),
    ("AB10048835", ("四獎", 4_000)), ("AB10008835", ("五獎", 1_000)), ("AB10000835", ("六獎", 200)),
    ("AB12345111", ("五獎", 1_000)),  # 末 4 碼 5111 對到 06595111
    ("AB00000000", None), ("壞掉", None),
])
def test_prize_tiers(inv, prize):
    assert L.parse_page(page("index")).check(inv) == prize


def test_dates_match_official_claim_windows():
    assert L.period_of(date(2026, 7, 1)) == L.period_of(date(2026, 8, 31)) == "11508"
    assert L.draw_date("11508") == date(2026, 9, 25)
    assert L.claim_window("11506") == (date(2026, 8, 6), date(2026, 11, 5))   # 頁面：115/08/06–115/11/05
    assert L.claim_window("11508") == (date(2026, 10, 6), date(2027, 1, 5))   # 頁面：115/10/06–116/01/05
    assert L.draw_date("11512") == date(2027, 1, 25)


def test_fetch_and_summarize(tmp_path):
    store = Store(tmp_path / "k.sqlite3")
    for n, (inv, d) in enumerate([("AB10000835", "2026-08-02"), ("CD12121212", "2026-08-03"),
                                  ("EF22222222", "2026-10-01"), ("GH33333333", "2026-04-01")]):
        y, m, dd = d.split("-")
        store.save({"invNum": inv, "amount": "100", "sellerName": "全家便利商店股份有限公司",
                    "invDate": {"year": str(int(y) - 1911), "month": m, "date": dd},
                    **({"awardInfo": "雲端發票專屬獎 500元"} if n == 1 else {})}, [])
    for draw in L.fetch_draws(lambda url: page("index" if url.endswith("index.html") else "lastNumber")):
        store.save_draw(draw)
    periods = {p["period"]: p for p in L.summarize(store.lottery_invoices(), store.draws(), date(2026, 10, 14))}
    aug = periods["11508"]
    assert aug["status"] == "drawn" and aug["claimable"] and aug["total"] == 200
    assert [w["prize"] for w in aug["winners"]] == ["六獎", "財政部標記中獎"]  # 雲端專屬獎照 awardInfo 顯示
    assert periods["11510"]["status"] == "pending" and periods["11510"]["draw_date"] == "2026-11-25"
    assert periods["11504"]["status"] == "expired"  # 官方只留兩期，更早的已過期


def _rows(*events):
    return [{"date": d, "seller": s, "description": desc, "amount": a, "n_items": n} for d, s, desc, a, n in events]


def test_detects_monthly_bimonthly_and_yearly():
    rows = _rows(
        *[(f"2026-{m:02d}-11", "酷澎股份有限公司", "WOW 會員訂閱服務月費", 59, 1) for m in (8, 9, 10)],
        *[(f"2026-{m:02d}-20", "台灣電力股份有限公司", "電費", a, 1) for m, a in ((4, 900), (6, 1300), (8, 1600))],
        ("2026-06-29", "新加坡商蝦皮娛樂電商有限公司台灣分公司", "蝦皮VIP訂閱-12個月方案", 365, 1),
    )
    found = {r.name: r for r in detect(rows, date(2026, 10, 14))}
    assert found["WOW 會員訂閱服務月費"].cadence == "每月"
    assert found["WOW 會員訂閱服務月費"].next_date == "2026-11-10"
    assert found["電費"].cadence == "每兩個月"  # 金額浮動，但是帳單
    assert found["蝦皮VIP訂閱-12個月方案"].cadence == "每年" and found["蝦皮VIP訂閱-12個月方案"].monthly_amount == 30


def test_ignores_groceries_irregular_and_stopped():
    rows = _rows(
        # 每月剛好在全聯買一次鮮奶，但都跟其他東西一起買：不是固定支出
        *[(f"2026-{m:02d}-03", "全聯實業股份有限公司", "鮮奶", 90, 6) for m in (5, 6, 7, 8, 9)],
        # 間隔不規則
        *[(d, "某某有限公司", "會員服務", 300, 1) for d in ("2026-05-01", "2026-05-20", "2026-07-30", "2026-08-05")],
        # 以前每月扣，三個月前停了
        *[(f"2026-{m:02d}-01", "某健身房", "會員月費", 999, 1) for m in (2, 3, 4)],
    )
    assert detect(rows, date(2026, 10, 14)) == []


def test_api_and_demo(tmp_path):
    build_demo_db(tmp_path / "d.sqlite3", today=date(2026, 10, 14)).close()
    web = TestClient(create_app(tmp_path / "d.sqlite3", demo=True), base_url="http://127.0.0.1")
    lot = web.get("/api/lottery").json()
    assert any(p["winners"] for p in lot["periods"]) and all(p["demo"] for p in lot["periods"] if p["winners"])
    rec = web.get("/api/recurring").json()
    assert {r["name"] for r in rec["items"]} >= {"會員月費", "電信月租費 4G 599 型", "電費"}
    assert [r["name"] for r in rec["stopped"]] == ["串流影音 標準方案 月費"]  # 展示「可能已經停了」
    assert rec["monthly_total"] > 0
    assert web.post("/api/lottery/refresh", headers={"X-KouKou": "1"}).status_code == 400  # 展示模式不連線


def test_fetch_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    from koukou.web import app as app_mod

    def boom(*a, **k):
        raise OSError("連不上 invoice.etax.nat.gov.tw")

    monkeypatch.setattr("koukou.lottery.fetch_draws", boom)
    store = Store(tmp_path / "k.sqlite3")
    store.save({"invNum": "AB10000835", "amount": "100", "sellerName": "全家",
                "invDate": {"year": "115", "month": "8", "date": "2"}}, [])
    store.close()
    job = app_mod.LotteryJob()
    job.run(tmp_path / "k.sqlite3", background=False)
    assert not job.state["fetching"] and "連不上" in job.state["error"]


COUPANG = [(f"2026-{m:02d}-11", "酷澎股份有限公司", "WOW 會員訂閱服務月費", 59, 1) for m in (7, 8, 9)]
GYM = [(f"2026-{m:02d}-01", "健身工廠", "會員月費", 1288, 1) for m in (7, 8, 9, 10)]


def test_stopped_subscription_is_flagged():
    # 10/12 前後該扣 Coupang，到 10/25 都沒看到（帳本有 10/20 的發票，代表有同步）
    rows = _rows(*COUPANG, *GYM, ("2026-10-20", "全家", "茶葉蛋", 13, 1))
    res = summary(rows, date(2026, 10, 25))
    assert [r["name"] for r in res["stopped"]] == ["WOW 會員訂閱服務月費"]
    assert "10/12" in res["stopped"][0]["message"] and "退訂" in res["stopped"][0]["message"]
    assert res["monthly_total"] == 1288  # 停掉的不算進每月總額
    # 才晚幾天，不要急著叫
    assert summary(rows, date(2026, 10, 15))["stopped"] == []


def test_not_flagged_when_ledger_is_just_stale():
    # 最後一次同步停在 10/1：10/11 沒看到 Coupang 是因為還沒抓，不是停了
    rows = _rows(*COUPANG, ("2026-10-01", "全家", "茶葉蛋", 13, 1))
    res = summary(rows, date(2026, 11, 30))
    assert res["stopped"] == [] and res["items"][0]["status"] == "active"


def test_strict_x509_is_relaxed_but_still_verified(tmp_path):
    """財政部的憑證鏈缺 Subject Key Identifier；Python 3.13+ 的嚴格模式會擋，我們只放寬這一條。"""
    import datetime as dt
    import http.server
    import ssl
    import threading
    import urllib.request

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.timezone.utc)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "No-SKI Test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))  # 故意不加 SubjectKeyIdentifier
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(2)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM)  # noqa: E731
    (tmp_path / "ca.pem").write_bytes(pem(ca))
    (tmp_path / "leaf.pem").write_bytes(pem(leaf) + leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(tmp_path / "leaf.pem")
    server.socket = sctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://localhost:{server.server_address[1]}/"
    try:
        ctx = L._ssl_context()
        ctx.load_verify_locations(tmp_path / "ca.pem")
        with urllib.request.urlopen(url, context=ctx, timeout=5) as r:
            assert r.read() == b"ok"
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname  # 其他檢查照舊
        if hasattr(ssl, "VERIFY_X509_STRICT") and ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT:
            strict = ssl.create_default_context()
            strict.load_verify_locations(tmp_path / "ca.pem")
            with pytest.raises(urllib.error.URLError, match="Key Identifier"):
                urllib.request.urlopen(url, context=strict, timeout=5)
        # 沒有信任這張 CA 的話，一樣要擋
        with pytest.raises(urllib.error.URLError, match="CERTIFICATE_VERIFY_FAILED"):
            urllib.request.urlopen(url, context=L._ssl_context(), timeout=5)
    finally:
        server.shutdown()
