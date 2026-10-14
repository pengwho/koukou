"""固定支出偵測：從發票找出「每隔固定時間、在同一家、買同一樣、金額差不多」的支出。

不用 AI，純統計：
1. 依（店家, 品名）分組；沒有品項的發票依（店家, 金額）分組
2. 至少出現 3 次，相鄰兩次的間隔要穩定（每個間隔都在中位數的 ±30% 內）
3. 間隔的中位數落在「每月」或「每兩個月」的範圍
4. 金額要穩定（變異係數 ≤ 15%）；電費、水費、電信這類帳單金額本來就會浮動，放寬到 40%
5. 訂閱和帳單通常一張發票只有它一項；大賣場裡剛好每月買一次的鮮奶不算。
   所以品名沒寫明月費／帳單的，至少 80% 要是「單品項發票」
6. 品名明寫「月費」「訂閱」「年費」的，次數門檻放寬：月費 2 次即可；年繳 1 次就列出

財政部只保留大約半年的發票，所以只抓得到月繳和兩個月繳的；年繳只能靠品名判斷。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from .store import merchant_name

CADENCES = [  # (名稱, 最短天數, 最長天數, 每月等值倍數)
    ("每月", 25, 36, 1.0),
    ("每兩個月", 54, 68, 0.5),
]
_SUBSCRIPTION = re.compile(r"月費|月租|訂閱|會員費|subscription|premium|方案", re.I)
_YEARLY = re.compile(r"年費|年繳|12\s*個月|一年")
_BILL = re.compile(r"電費|水費|瓦斯|電信|電話|網路|寬頻|台電|自來水|中華電信|台灣大哥大|遠傳")


@dataclass
class Recurring:
    name: str
    merchant: str
    cadence: str
    typical_amount: int
    monthly_amount: int
    count: int
    last_date: str
    next_date: str
    reason: str  # 為什麼判定是固定支出，顯示在畫面上


def _amount_cv(amounts: list[float]) -> float:
    mean = statistics.fmean(amounts)
    return statistics.pstdev(amounts) / mean if mean else 1.0


def _cadence(days: float) -> tuple[str, float] | None:
    for name, lo, hi, per_month in CADENCES:
        if lo <= days <= hi:
            return name, per_month
    return None


def detect(rows: list[dict[str, Any]], today: date) -> list[Recurring]:
    """rows：每筆是一次消費，需要 date (YYYY-MM-DD)、seller、description（可為 None）、amount、
    n_items（那張發票有幾個品項）。"""
    groups: dict[tuple[str, str], list[tuple[date, float, str, bool]]] = {}
    for r in rows:
        if not r.get("date") or not r.get("amount") or r["amount"] <= 0:
            continue
        shop = merchant_name(r.get("seller"))
        desc = (r.get("description") or "").strip()
        key = (shop, desc or f"${round(r['amount'])}")
        single = (r.get("n_items") or 1) <= 1
        groups.setdefault(key, []).append((date.fromisoformat(r["date"]), float(r["amount"]), desc, single))

    found: list[Recurring] = []
    for (shop, _label), events in groups.items():
        # 同一天買兩次算一次
        by_day: dict[date, float] = {}
        for d, amt, _, _ in events:
            by_day[d] = by_day.get(d, 0) + amt
        days = sorted(by_day)
        amounts = [by_day[d] for d in days]
        desc = events[0][2]
        text = f"{desc} {shop}"
        is_sub, is_year = bool(_SUBSCRIPTION.search(text)), bool(_YEARLY.search(text))
        is_bill = bool(_BILL.search(text))
        name = desc or f"{shop} 固定金額 ${round(amounts[0])}"

        if is_year and len(days) == 1:
            found.append(Recurring(name, shop, "每年", round(amounts[0]), round(amounts[0] / 12), 1,
                                   days[0].isoformat(), (days[0] + timedelta(days=365)).isoformat(),
                                   "品名寫著年繳"))
            continue
        need = 2 if is_sub else 3
        if len(days) < need:
            continue
        single_ratio = sum(e[3] for e in events) / len(events)
        if not (is_sub or is_bill) and single_ratio < 0.8:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
        median_gap = statistics.median(gaps)
        cadence = _cadence(median_gap)
        if not cadence or any(abs(g - median_gap) > 0.3 * median_gap for g in gaps):
            continue
        cv_limit = 0.4 if is_bill else 0.15
        if _amount_cv(amounts) > cv_limit:
            continue
        typical = statistics.median(amounts)
        last = days[-1]
        nxt = last + timedelta(days=round(median_gap))
        if (today - last).days > 2 * median_gap:  # 很久沒出現，可能已經停了
            continue
        reasons = [f"{len(days)} 次，間隔約 {round(median_gap)} 天"]
        if is_sub:
            reasons.append("品名有月費／訂閱")
        if is_bill:
            reasons.append("帳單金額會浮動")
        found.append(Recurring(name, shop, cadence[0], round(typical), round(typical * cadence[1]), len(days),
                               last.isoformat(), nxt.isoformat(), "，".join(reasons)))
    return sorted(found, key=lambda r: -r.monthly_amount)


def summary(rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    items = detect(rows, today)
    return {
        "items": [asdict(r) for r in items],
        "monthly_total": sum(r.monthly_amount for r in items),
        "upcoming": [asdict(r) for r in sorted(items, key=lambda r: r.next_date)
                     if 0 <= (date.fromisoformat(r.next_date) - today).days <= 14],
    }
