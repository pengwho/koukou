"""阿財的內建小模型：字元 n-gram + 店名的多項式 Naive Bayes，純 Python、不用網路也不用金鑰。

為什麼是 Naive Bayes：
- 發票品名很短、很雜（「FP蘇打餅」「光泉290ml」），字元 n-gram 比斷詞穩
- 訓練幾百筆不到 0.1 秒，所以每次分類前都能把「你親手改過的分類」加進去重新訓練，
  不需要存模型檔，也不會有模型跟程式版本對不上的問題
- 店名當成一個特徵：同樣是「霜淇淋」，在 IKEA 跟在全家分到的類別一樣，但「月費」在健身房是娛樂

信心值是把各類別的 log 機率除以溫度 T 再做 softmax；Naive Bayes 原始機率通常過度自信，
T 用交叉驗證挑，讓「信心 0.6」大致等於「十次對六次」。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .categories import CATEGORY_IDS, normalize

TRAINSET = Path(__file__).with_name("trainset.txt")
STORE_WEIGHT = 3  # 店名特徵的權重（相當於出現幾次）
ALPHA = 0.3       # 平滑


@dataclass(frozen=True)
class Example:
    text: str
    store: str
    category: str
    weight: float = 1.0


def load_trainset(path: Path = TRAINSET) -> list[Example]:
    out, category = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            category = line[1:]
            continue
        store, _, item = line.partition("|")
        out.append(Example(item, store, category))
    return out


def features(text: str, store: str = "") -> Counter[str]:
    t = re.sub(r"\d+", "0", normalize(text)).replace(" ", "")
    t = f"^{t}$"
    feats: Counter[str] = Counter()
    for n in (1, 2, 3):
        for i in range(len(t) - n + 1):
            gram = t[i:i + n]
            if gram not in ("^", "$"):
                feats[gram] += 1
    if store:
        name = normalize(store)
        feats["店:" + name] += STORE_WEIGHT
        # 店名的兩字片段：沒看過的店，也能從「餐飲」「飲食」「壽司」「藥局」這類字眼猜出是哪種店
        for i in range(len(name) - 1):
            feats["店2:" + name[i:i + 2]] += 1
    return feats


class NaiveBayes:
    def __init__(self, examples: Iterable[Example], temperature: float = 1.0) -> None:
        self.temperature = temperature
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        docs: Counter[str] = Counter()
        for ex in examples:
            for f, c in features(ex.text, ex.store).items():
                counts[ex.category][f] += c * ex.weight
            docs[ex.category] += ex.weight
        self.classes = [c for c in CATEGORY_IDS if docs[c] > 0]
        self.vocab = {f for c in counts.values() for f in c}
        v = len(self.vocab)
        total_docs = sum(docs.values())
        # 先驗開根號壓平，避免樣本多的類別（餐飲、生鮮）吃掉所有模稜兩可的品項
        self.log_prior = {c: 0.5 * math.log(docs[c] / total_docs) for c in self.classes}
        self.log_lik: dict[str, dict[str, float]] = {}
        self.log_unseen: dict[str, float] = {}
        for c in self.classes:
            denom = sum(counts[c].values()) + ALPHA * v
            self.log_lik[c] = {f: math.log((n + ALPHA) / denom) for f, n in counts[c].items()}
            self.log_unseen[c] = math.log(ALPHA / denom)

    def scores(self, text: str, store: str = "", exclude: frozenset[str] = frozenset()) -> dict[str, float]:
        feats = {f: n for f, n in features(text, store).items() if f in self.vocab}
        out = {}
        for c in self.classes:
            if c in exclude:
                continue
            lik, unseen = self.log_lik[c], self.log_unseen[c]
            out[c] = self.log_prior[c] + sum(n * lik.get(f, unseen) for f, n in feats.items())
        return out

    def predict(self, text: str, store: str = "", exclude: frozenset[str] = frozenset()) -> tuple[str, float]:
        """回傳 (分類, 信心 0–1)。完全沒有認得的特徵時回傳 ("其他", 0)。"""
        if not any(f in self.vocab for f in features(text, store)):
            return "其他", 0.0
        s = self.scores(text, store, exclude)
        top = max(s, key=s.get)
        z = [math.exp((v - s[top]) / self.temperature) for v in s.values()]
        return top, 1.0 / sum(z)


def cross_validate(examples: list[Example], folds: int = 5, temperature: float = 1.0) -> tuple[float, float]:
    """回傳 (準確率, 平均 log 機率)，用來挑溫度。"""
    correct, loglik = 0, 0.0
    for k in range(folds):
        train = [e for i, e in enumerate(examples) if i % folds != k]
        test = [e for i, e in enumerate(examples) if i % folds == k]
        model = NaiveBayes(train, temperature)
        for e in test:
            s = model.scores(e.text, e.store)
            top = max(s, key=s.get)
            correct += top == e.category
            z = sum(math.exp((v - s[top]) / temperature) for v in s.values())
            p = math.exp((s.get(e.category, -1e9) - s[top]) / temperature) / z
            loglik += math.log(max(p, 1e-9))
    return correct / len(examples), loglik / len(examples)


@lru_cache(maxsize=1)
def calibrated_temperature() -> float:
    examples = load_trainset()
    best = max((cross_validate(examples, temperature=t)[1], t) for t in (1, 2, 3, 4, 6, 8, 12))
    return best[1]


def build(extra: Iterable[Example] = ()) -> NaiveBayes:
    """內建訓練資料 + 額外範例（使用者的修正權重高，會蓋過內建資料的習慣）。"""
    return NaiveBayes([*load_trainset(), *extra], calibrated_temperature())
