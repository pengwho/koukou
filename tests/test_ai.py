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


def test_rules_on_evalset_are_never_wrong():
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
    out, stats = classify([Item("運費"), Item("茶葉蛋")], fake, llm_threshold=1.01)
    assert (out[0].source, out[1].source) == ("rule", "llm")
    assert stats.rule == 1 and stats.llm == 1
    assert "運費" not in fake.prompts[0]  # 規則能判斷的不會送給模型


def _store_with(tmp_path, items):
    store = Store(tmp_path / "k.sqlite3")
    header = {"invNum": "AA00000001", "amount": str(sum(a for _, a in items)), "sellerName": "全聯實業股份有限公司",
              "invDate": {"year": "115", "month": "10", "date": "1"}}
    store.save(header, [{"rowNum": str(i + 1), "description": d, "amount": str(a)} for i, (d, a) in enumerate(items)])
    return store


def test_categorize_store_works_without_any_ai_and_respects_user(tmp_path):
    store = _store_with(tmp_path, [("鮮奶", 90), ("鮮奶", 90), ("促銷折扣", -10), ("霜淇淋", 30)])
    store.set_user_category("霜淇淋", "零食點心")
    stats = categorize_store(store, None)  # 沒有金鑰、沒有網路
    assert stats.rule == 1 and stats.local == 1 and stats.llm == 0  # 兩筆鮮奶算一個品名
    assert store.category_stats()["pending"] == 0
    store.save_categories([(normalize("霜淇淋"), "霜淇淋", "其他", "llm", 0.9, "x")])
    assert store.items("AA00000001")[3]["category"] == "零食點心"  # 使用者的修正不會被覆蓋


def test_user_corrections_retrain_local_model(tmp_path):
    store = _store_with(tmp_path, [("阿嬤的私房醬菜", 120), ("阿嬤的私房醬菜 小", 80)])
    store.set_user_category("阿嬤的私房醬菜", "娛樂")  # 刻意改成很少見的分類
    categorize_store(store, None)
    assert store.items("AA00000001")[1]["category"] == "娛樂"  # 相似品名跟著你的習慣走


def test_llm_only_reviews_unsure_local_guesses(tmp_path):
    store = _store_with(tmp_path, [("鮮奶", 90), ("霜淇淋", 30)])
    store.set_user_category("霜淇淋", "零食點心")
    fake = FakeLLM({"鮮奶": "生鮮雜貨"})
    out, stats = classify([Item("鮮奶", "全聯", 90), Item("運費")], fake, store.user_examples(),
                          llm_threshold=1.01)  # 門檻設超高：小模型的答案全部送 LLM 複查
    assert [g.source for g in out] == ["llm", "rule"] and stats.llm == 1
    assert "霜淇淋 → 零食點心" in fake.prompts[0] and "運費" not in fake.prompts[0]
    _, stats = classify([Item("鮮奶", "全聯", 90)], FakeLLM(), llm_threshold=0)
    assert stats.local == 1 and stats.llm == 0  # 門檻 0：小模型全權處理，不花錢


def test_positive_items_are_never_discounts():
    out, _ = classify([Item("滿千折百", "家樂福", 199)], None)
    assert out[0].category != "折扣"


def test_offline_accuracy_floor():
    """沒有任何金鑰時，規則＋內建小模型在保留測試集上的準確率不能退步。"""
    rep = evaluate(None)
    assert rep.unanswered == 0
    assert rep.accuracy >= 0.85, rep.accuracy


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
    rep = evaluate(fake, rows, llm_threshold=1.01)
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
    assert not ai["configured"] and ai["stats"]["pending"] == 2  # 還沒跑過分類
    bad = web.post("/api/items/category", headers=H, json={"description": "鮮奶", "category": "亂填"})
    assert bad.status_code == 422
    r = web.post("/api/items/category", headers=H, json={"description": "鮮奶", "category": "生鮮雜貨"})
    assert r.json()["applied_to"] == 1
    assert web.get("/api/ai").json()["stats"]["user"] == 1
    cats = {c["category"] for c in web.get("/api/breakdown", params={"month": "2026-10"}).json()}
    assert cats == {"生鮮雜貨", "零食點心"}  # 改一個之後，內建小模型順便把其他品項也分好了


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
    assert state["result"]["rule"] + state["result"]["local"] + state["result"]["llm"] == 2
    assert state["stats"]["pending"] == 0


def test_review_queue_orders_by_money_and_learns(tmp_path):
    store = Store(tmp_path / "k.sqlite3")
    for n, (desc, amt) in enumerate([("神秘禮盒 A款", 900), ("神秘禮盒 B款", 500), ("小東西", 30)]):
        store.save({"invNum": f"AA0000000{n}", "amount": str(amt), "sellerName": "某某有限公司",
                    "invDate": {"year": "115", "month": "10", "date": "1"}},
                   [{"rowNum": "1", "description": desc, "amount": str(amt)}])
    names = ("神秘禮盒 A款", "神秘禮盒 B款", "小東西")
    store.save_categories([(normalize(d), d, "未分類", "local", 0.2, "") for d in names])
    queue = store.review_queue()
    assert [q["description"] for q in queue] == ["神秘禮盒 A款", "神秘禮盒 B款", "小東西"]  # 金額大的先問
    store.set_user_category("神秘禮盒 A款", "娛樂")
    categorize_store(store, None)  # 重新訓練：相似品名跟著學
    assert store.items("AA00000001")[0]["category"] == "娛樂"
    assert [q["description"] for q in store.review_queue()] == ["小東西"]


def test_api_correction_reports_propagation(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    store = Store(tmp_path / "k.sqlite3")
    for n, desc in enumerate(["神秘禮盒 A款", "神秘禮盒 B款"]):
        store.save({"invNum": f"AA0000000{n}", "amount": "100", "sellerName": "某某有限公司",
                    "invDate": {"year": "115", "month": "10", "date": "1"}},
                   [{"rowNum": "1", "description": desc, "amount": "100"}])
    store.save_categories([(normalize(d), d, "未分類", "local", 0.2, "") for d in ("神秘禮盒 A款", "神秘禮盒 B款")])
    store.close()
    web = TestClient(create_app(tmp_path / "k.sqlite3"), base_url="http://127.0.0.1")
    assert len(web.get("/api/ai/review").json()) == 2
    fix = {"description": "神秘禮盒 A款", "category": "娛樂"}
    r = web.post("/api/items/category", headers={"X-KouKou": "1"}, json=fix)
    assert r.json() == {"applied_to": 1, "also_resolved": 1}
    assert web.get("/api/ai/review").json() == []
