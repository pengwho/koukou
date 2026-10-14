"""阿財的分類流程：規則 → 內建小模型 → （有設定的話）LLM 只處理小模型沒把握的。

- 不需要任何金鑰或網路：規則加內建小模型就能分完全部品項。
- 同一個品名（正規化後）的結果存在 item_categories，之後所有同名品項共用。
- 使用者手動改過的分類優先權最高，也會被當成 few-shot 範例，讓阿財學你的習慣。
- 送給模型的只有「品名」和「店名」，不送金額、日期、發票號碼或載具。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import local_model
from .categories import CATEGORIES, CATEGORY_IDS, UNCATEGORIZED, rule_category
from .llm import LLMError, Provider, Usage

log = logging.getLogger("koukou.ai")

BATCH_SIZE = 40
MAX_EXAMPLES = 30
LLM_THRESHOLD = 0.75  # 小模型信心低於這個才請 LLM 再看一次
UNSURE = 0.6          # 畫面上用虛線標出沒把握的分類，也會列進「請你確認」
ABSTAIN = 0.3         # 低於這個就不硬猜，標成未分類

SYSTEM_PROMPT = (
    "你是台灣記帳工具「摳摳」裡的分類小幫手阿財。使用者會給你電子發票上的品項名稱和店名，"
    "請把每個品項分到下列其中一類：\n"
    + "\n".join(f"- {c.id}：{c.hint}" for c in CATEGORIES)
    + "\n\n注意：\n"
    "- category 只能填上面的分類名稱，一字不差。\n"
    "- 發票品名常是縮寫、規格或店內代號（例如「FP蘇打餅」「光泉290ml」），請搭配店名判斷"
    "（例如「霜淇淋」在 IKEA 是零食點心，「烤地瓜」在超商是零食點心）。\n"
    "- confidence 是 0 到 1 的把握程度；完全看不出是什麼商品時，選「其他」並給低分。\n"
    "- 每個 id 都要回傳，不要漏掉。"
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORY_IDS)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["id", "category", "confidence"],
            },
        }
    },
    "required": ["results"],
}


@dataclass
class Item:
    description: str
    store: str = ""
    amount: float | None = None


@dataclass
class Guess:
    category: str
    confidence: float
    source: str  # rule / local / llm / user / demo


@dataclass
class RunStats:
    rule: int = 0
    local: int = 0
    llm: int = 0
    failed: int = 0
    usage: Usage = field(default_factory=Usage)


def build_user_prompt(items: list[Item], examples: list[tuple[str, str]]) -> str:
    parts = []
    if examples:
        parts.append("使用者以前親手改過的分類，請照這個習慣：")
        parts += [f"- {desc} → {cat}" for desc, cat in examples[:MAX_EXAMPLES]]
        parts.append("")
    parts.append("請分類以下品項：")
    parts.append(json.dumps(
        [{"id": i, "item": it.description, "store": it.store} for i, it in enumerate(items)],
        ensure_ascii=False, indent=0,
    ))
    return "\n".join(parts)


def classify_with_llm(
    provider: Provider, items: list[Item], examples: list[tuple[str, str]] | None = None,
) -> tuple[list[Guess | None], Usage]:
    """一批送給模型；回傳跟 items 對齊的結果，模型漏掉的位置是 None。"""
    usage = Usage()
    out: list[Guess | None] = [None] * len(items)
    pending = list(range(len(items)))
    for attempt in range(2):  # 漏掉的再補問一次
        if not pending:
            break
        batch = [items[i] for i in pending]
        try:
            data, u = provider.classify(SYSTEM_PROMPT, build_user_prompt(batch, examples or []), SCHEMA)
        except LLMError as e:
            log.warning("LLM batch failed (attempt %d): %s", attempt + 1, e)
            if attempt == 1:
                raise
            continue
        usage.add(u)
        for r in data.get("results") or []:
            try:
                idx = int(r["id"])
                target = pending[idx]
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            category = r.get("category")
            conf = float(r.get("confidence") or 0)
            if category not in CATEGORY_IDS:
                category, conf = "其他", 0.0
            out[target] = Guess(category, max(0.0, min(conf, 1.0)), "llm")
        pending = [i for i in pending if out[i] is None]
    return out, usage


def classify(
    items: list[Item],
    provider: Provider | None,
    examples: list[tuple[str, str]] | None = None,
    batch_size: int = BATCH_SIZE,
    progress: Callable[[int, int], None] = lambda done, total: None,
    extra_training: list[local_model.Example] | None = None,
    llm_threshold: float = LLM_THRESHOLD,
) -> tuple[list[Guess | None], RunStats]:
    """完整 cascade（不含快取）：規則 → 內建小模型 → LLM（只看小模型沒把握的）。評估也用這個函式。"""
    stats = RunStats()
    out: list[Guess | None] = [None] * len(items)
    model = local_model.build([
        *(local_model.Example(d, "", c, weight=5.0) for d, c in (examples or [])),  # 你的修正最優先
        *(extra_training or []),
    ])
    for i, it in enumerate(items):
        cat = rule_category(it.description, it.amount)
        if cat:
            out[i] = Guess(cat, 1.0, "rule")
            stats.rule += 1
            continue
        # 正數品項不可能是折扣；被分成折扣的話金額會被攤掉，所以直接排除
        exclude = frozenset({"折扣"}) if it.amount is not None and it.amount > 0 else frozenset()
        cat, conf = model.predict(it.description, it.store, exclude)
        out[i] = Guess(cat if conf >= ABSTAIN else UNCATEGORIZED, conf, "local")
        stats.local += 1
    todo = [i for i, g in enumerate(out) if g and g.source == "local" and g.confidence < llm_threshold]
    if provider is None or not todo:
        return out, stats
    for start in range(0, len(todo), batch_size):
        chunk = todo[start:start + batch_size]
        guesses, usage = classify_with_llm(provider, [items[i] for i in chunk], examples)
        stats.usage.add(usage)
        for i, g in zip(chunk, guesses, strict=True):
            if g:  # LLM 沒回答的就保留小模型的答案
                out[i] = g
                stats.llm += 1
                stats.local -= 1
        progress(min(start + batch_size, len(todo)), len(todo))
    return out, stats


def categorize_store(
    store, provider: Provider | None, progress: Callable[[str], None] = lambda msg: None,
) -> RunStats:
    """把帳本裡還沒分類的品名分類好，寫回快取表。"""
    # 小模型的舊答案每次都重算：你剛改過的分類會馬上影響相似的品名
    pending = store.pending_items(include_local=True)  # [(key, 品名, 店名, 最大金額)]
    if not pending:
        return RunStats()
    items = [Item(desc, shop, amount) for _, desc, shop, amount in pending]
    progress(f"整理 {len(items)} 個品名")
    guesses, stats = classify(
        items, provider, store.user_examples(200),
        progress=lambda done, total: progress(f"請 AI 複查沒把握的 {done}/{total}"),
        extra_training=[local_model.Example(d, "", c) for d, c in store.confident_llm_labels()],
    )
    model = getattr(provider, "model", "") if provider else ""
    store.save_categories([
        (key, desc, g.category, g.source, g.confidence, model if g.source == "llm" else "")
        for (key, desc, _, _), g in zip(pending, guesses, strict=True) if g
    ])
    return stats
