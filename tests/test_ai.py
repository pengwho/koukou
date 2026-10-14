"""阿財：規則、LLM 批次（假模型）、快取、使用者修正、攤提、評估、API。"""

import json
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from koukou import credentials
from koukou.ai import llm
from koukou.ai.categories import normalize, rule_category
from koukou.ai.categorizer import SCHEMA, Item, categorize_store, classify, classify_with_llm
from koukou.ai.evaluate import evaluate, load_evalset
from koukou.demo import build_demo_db
from koukou.store import Store
from koukou.web.app import create_app


class FakeLLM:
    """照字典回答的假模型；可設定漏答與亂答，並記錄收到的提示詞。"""

    name, model = "fake", "fake-1"

    def __init__(self, answers=None, drop_first=False, bogus=()):
        self.answers = answers or {}
        self.drop_first = drop_first
        self.bogus = set(bogus)
        self.prompts = []

    def classify(self, system, user, schema):
        assert schema == SCHEMA
        self.prompts.append(user)
        items = json.loads(user[user.index("["):])
        results = []
        for it in items:
            if self.drop_first and len(self.prompts) == 1 and it["id"] == 0:
                continue  # 第一次故意漏掉，測補問
            cat = "不存在的分類" if it["item"] in self.bogus else self.answers.get(it["item"], "其他")
            results.append({"id": it["id"], "category": cat, "confidence": 0.9})
        return {"results": results}, llm.Usage(100, 20, 1, 0.01)


def test_rules_are_precise():
    assert rule_category("促銷折扣", -20) == "折扣"
    assert rule_category("優惠組洗髮精", 299) is None  # 正價商品不是折扣
    assert rule_category("ＣＩＴＹ　ＣＡＦＥ 大杯拿鐵") == "咖啡飲料"  # 全形也認得
    assert rule_category("咖啡豆 半磅") is None
    assert normalize("  Ｃｏｋｅ   Zero ") == "coke zero"


def test_rules_baseline_on_evalset_is_never_wrong():
    rep = evaluate(None)
    assert rep.total >= 100
    assert rep.rule_covered > 0 and rep.rule_correct == rep.rule_covered


def test_llm_retries_missing_and_rejects_bogus_categories():
    fake = FakeLLM({"茶葉蛋": "生鮮雜貨", "霜淇淋": "零食點心"}, drop_first=True, bogus={"謎之商品"})
    out, usage = classify_with_llm(fake, [Item("茶葉蛋"), Item("霜淇淋"), Item("謎之商品")])
    assert [g.category for g in out] == ["生鮮雜貨", "零食點心", "其他"]
    assert out[2].confidence == 0
    assert usage.calls == 2 and len(fake.prompts) == 2


def test_classify_uses_rules_before_llm():
    fake = FakeLLM({"茶葉蛋": "生鮮雜貨"})
    out, stats = classify([Item("運費"), Item("茶葉蛋")], fake)
    assert (out[0].source, out[1].source) == ("rule", "llm")
    assert stats.rule == 1 and stats.llm == 1
    assert "運費" not in fake.prompts[0]  # 規則能判斷的不會送給模型


def _store_with(tmp_path, items):
    store = Store(tmp_path / "k.sqlite3")
    header = {"invNum": "AA00000001", "amount": str(sum(a for _, a in items)), "sellerName": "全聯實業股份有限公司",
              "invDate": {"year": "115", "month": "10", "date": "1"}}
    store.save(header, [{"rowNum": str(i + 1), "description": d, "amount": str(a)} for i, (d, a) in enumerate(items)])
    return store


def test_categorize_store_caches_by_name_and_respects_user(tmp_path):
    store = _store_with(tmp_path, [("鮮奶", 90), ("鮮奶", 90), ("促銷折扣", -10), ("霜淇淋", 30)])
    store.set_user_category("霜淇淋", "零食點心")
    fake = FakeLLM({"鮮奶": "生鮮雜貨"})
    stats = categorize_store(store, fake)
    assert stats.llm == 1 and stats.rule == 1  # 兩筆鮮奶只問一次；折扣走規則；使用者改過的不重問
    assert "使用者以前親手改過" in fake.prompts[0] and "霜淇淋 → 零食點心" in fake.prompts[0]
    assert store.category_stats()["pending"] == 0
    assert categorize_store(store, fake).llm == 0  # 第二次全部命中快取
    store.save_categories([(normalize("霜淇淋"), "霜淇淋", "其他", "llm", 0.9, "x")])
    assert store.items("AA00000001")[3]["category"] == "零食點心"  # 使用者的修正不會被模型覆蓋


def test_breakdown_allocates_discounts_and_sums_to_total(tmp_path):
    store = _store_with(tmp_path, [("鮮奶", 90), ("洗碗精", 110), ("促銷折扣", -20)])
    store.save_categories([(normalize("鮮奶"), "鮮奶", "生鮮雜貨", "llm", 1, ""),
                           (normalize("洗碗精"), "洗碗精", "日用品", "rule", 1, "")])
    rows = {r["category"]: r["amount"] for r in store.breakdown("2026-10")}
    assert rows == {"生鮮雜貨": 81, "日用品": 99}  # 折扣 20 依比例攤回，合計 180
    assert store.invoices("2026-10", category="日用品")[0]["inv_num"] == "AA00000001"


def test_demo_breakdown_matches_monthly_totals(tmp_path):
    store = build_demo_db(tmp_path / "d.sqlite3", today=date(2026, 10, 14))
    for m in store.monthly():
        rows = store.breakdown(m["month"])
        assert abs(sum(r["amount"] for r in rows) - m["amount"]) <= len(rows)  # 只差在各分類四捨五入
    assert store.category_stats()["pending"] == 0


def test_evaluate_with_perfect_fake_model():
    rows = load_evalset()
    fake = FakeLLM({r["item"]: r["category"] for r in rows})
    rep = evaluate(fake, rows)
    assert rep.correct == rep.total and not rep.confusions
    assert rep.calls >= 1 and rep.input_tokens > 0


def test_store_survives_concurrent_requests(tmp_path):
    """回歸測試：共用連線 + Python 自訂函式在多執行緒下曾經互相卡死。"""
    store = build_demo_db(tmp_path / "d.sqlite3", today=date(2026, 10, 14))
    errors = []

    def hammer():
        try:
            for _ in range(20):
                store.breakdown("2026-10")
                store.merchants("2026-10")
                store.invoices("2026-10", category="餐飲")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and not any(t.is_alive() for t in threads)


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    credentials._secrets.clear()
    store = _store_with(tmp_path, [("鮮奶", 90), ("霜淇淋", 30)])
    store.close()
    return TestClient(create_app(tmp_path / "k.sqlite3"), base_url="http://127.0.0.1")


def test_api_correction_and_status(web):
    H = {"X-KouKou": "1"}
    ai = web.get("/api/ai").json()
    assert not ai["configured"] and ai["stats"]["pending"] == 2
    bad = web.post("/api/items/category", headers=H, json={"description": "鮮奶", "category": "亂填"})
    assert bad.status_code == 422
    r = web.post("/api/items/category", headers=H, json={"description": "鮮奶", "category": "生鮮雜貨"})
    assert r.json() == {"applied_to": 1}
    assert web.get("/api/ai").json()["stats"]["user"] == 1
    cats = {c["category"] for c in web.get("/api/breakdown", params={"month": "2026-10"}).json()}
    assert cats == {"生鮮雜貨", "未分類"}


def test_api_categorize_runs_with_configured_provider(web, monkeypatch):
    fake = FakeLLM({"鮮奶": "生鮮雜貨", "霜淇淋": "零食點心"})
    monkeypatch.setattr(llm, "from_environment",
                        lambda: llm.ProviderInfo(True, "fake", "fake-1", "", fake))
    assert web.post("/api/ai/categorize", headers={"X-KouKou": "1"}).status_code == 200
    for _ in range(100):
        state = web.get("/api/ai").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["result"]["llm"] == 2 and state["stats"]["pending"] == 0
