"""用人工標註的測試集評估阿財：規則層的覆蓋率與準確率、LLM 的準確率、常見誤判、token 用量。

    koukou ai eval                 # 規則 + 內建小模型 +（有設定的話）LLM
    koukou ai eval --offline       # 只量不需要網路的部分：規則 + 內建小模型
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .categorizer import Item, classify
from .llm import Provider

EVALSET = Path(__file__).with_name("evalset.jsonl")


@dataclass
class Report:
    model: str
    total: int
    correct: int
    rule_covered: int
    rule_correct: int
    local_covered: int
    local_correct: int
    llm_covered: int
    llm_correct: int
    unanswered: int
    abstained: int = 0
    per_category: dict[str, dict[str, int]] = field(default_factory=dict)
    confusions: list[tuple[str, str, str, str]] = field(default_factory=list)  # (店, 品名, 標準答案, 猜的)
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0
    wrong_confidence: float | None = None
    right_confidence: float | None = None

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def load_evalset(path: Path = EVALSET) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(provider: Provider | None, rows: list[dict] | None = None, llm_threshold: float | None = None) -> Report:
    rows = rows if rows is not None else load_evalset()
    items = [Item(r["item"], r["store"]) for r in rows]
    kwargs = {} if llm_threshold is None else {"llm_threshold": llm_threshold}
    guesses, stats = classify(items, provider, **kwargs)
    rep = Report(
        model=getattr(provider, "model", "") if provider else "離線（規則＋內建小模型）",
        total=len(rows), correct=0, rule_covered=0, rule_correct=0, local_covered=0, local_correct=0,
        llm_covered=0, llm_correct=0, unanswered=0,
        input_tokens=stats.usage.input_tokens, output_tokens=stats.usage.output_tokens,
        calls=stats.usage.calls, seconds=round(stats.usage.seconds, 2),
    )
    right_conf, wrong_conf = [], []
    for row, g in zip(rows, guesses, strict=True):
        gold = row["category"]
        cat = rep.per_category.setdefault(gold, {"total": 0, "correct": 0})
        cat["total"] += 1
        if g is None:
            rep.unanswered += 1
            continue
        if g.category == "未分類":
            rep.abstained += 1
        ok = g.category == gold
        rep.correct += ok
        cat["correct"] += ok
        if g.source == "rule":
            rep.rule_covered += 1
            rep.rule_correct += ok
        elif g.source == "local":
            rep.local_covered += 1
            rep.local_correct += ok
        else:
            rep.llm_covered += 1
            rep.llm_correct += ok
            (right_conf if ok else wrong_conf).append(g.confidence)
        if not ok:
            rep.confusions.append((row["store"], row["item"], gold, g.category))
    if right_conf:
        rep.right_confidence = round(sum(right_conf) / len(right_conf), 3)
    if wrong_conf:
        rep.wrong_confidence = round(sum(wrong_conf) / len(wrong_conf), 3)
    return rep


def _pct(a: int, b: int) -> str:
    return f"{a / b:6.1%}" if b else "     -"


def format_report(rep: Report) -> str:
    lines = [
        f"阿財分類評估（{rep.total} 個人工標註品項，模型：{rep.model}）",
        "",
        f"  整體準確率   {_pct(rep.correct, rep.total)}  ({rep.correct}/{rep.total})",
        f"  規則層       覆蓋 {_pct(rep.rule_covered, rep.total)}，準確 {_pct(rep.rule_correct, rep.rule_covered)}",
    ]
    local_acc = _pct(rep.local_correct, rep.local_covered)
    lines.append(f"  內建小模型   負責 {_pct(rep.local_covered, rep.total)}，準確 {local_acc}")
    if rep.llm_covered:
        llm_acc = _pct(rep.llm_correct, rep.llm_covered)
        lines.append(f"  LLM 層       準確 {llm_acc}  ({rep.llm_correct}/{rep.llm_covered})")
        lines.append(f"  平均信心     答對 {rep.right_confidence}，答錯 {rep.wrong_confidence}")
        tokens = f"輸入 {rep.input_tokens:,} / 輸出 {rep.output_tokens:,} tokens"
        lines.append(f"  用量         {rep.calls} 次呼叫，{tokens}，{rep.seconds} 秒")
    if rep.abstained:
        lines.append(f"  沒把握不猜   {rep.abstained} 個（標成未分類，算答錯）")
    if rep.unanswered:
        lines.append(f"  沒有答案     {rep.unanswered} 個（規則判斷不了，且沒有設定模型）")
    lines += ["", "  各分類召回率"]
    for name, c in sorted(rep.per_category.items(), key=lambda kv: -kv[1]["total"]):
        lines.append(f"    {name:<6}\t{_pct(c['correct'], c['total'])}  ({c['correct']}/{c['total']})")
    if rep.confusions:
        lines += ["", "  分錯的品項（店｜品名：標準答案 → 阿財）"]
        lines += [f"    {s}｜{i}：{g} → {p}" for s, i, g, p in rep.confusions[:25]]
        common = Counter((g, p) for _, _, g, p in rep.confusions).most_common(3)
        lines.append("  最常混淆：" + "、".join(f"{g}→{p}（{n}）" for (g, p), n in common))
    return "\n".join(lines)


def save_report(rep: Report, path: Path) -> None:
    data = asdict(rep) | {"accuracy": rep.accuracy}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
