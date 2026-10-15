"""自動對獎：抓財政部公布的中獎號碼，比對帳本裡每一張發票。

規則（財政部稅務入口網 invoice.etax.nat.gov.tw）：
- 特別獎 1,000 萬：8 位數跟特別獎號碼相同
- 特獎 200 萬：8 位數跟特獎號碼相同
- 頭獎 20 萬：8 位數跟任一頭獎號碼相同
- 二獎到六獎：末 7／6／5／4／3 位跟任一頭獎號碼相同，各 4 萬／1 萬／4 千／1 千／2 百
- 增開六獎（目前沒有開，但保留支援）：末 3 位相同，2 百

雲端發票專屬獎每期開出幾百萬組，只公布 PDF，沒辦法在本機比對；
財政部會在發票資料的 awardInfo 標示，這裡照實顯示。

開獎日：期別最後一個月的下個月 25 日（例如 7–8 月期在 9 月 25 日開獎）。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

log = logging.getLogger("koukou.lottery")

BASE_URL = "https://invoice.etax.nat.gov.tw/"
PAGES = ("index.html", "lastNumber.html")  # 最新一期、上一期

PRIZES = [  # (名稱, 獎金)
    ("特別獎", 10_000_000), ("特獎", 2_000_000), ("頭獎", 200_000), ("二獎", 40_000),
    ("三獎", 10_000), ("四獎", 4_000), ("五獎", 1_000), ("六獎", 200),
]
_FIRST_TIERS = [("頭獎", 8), ("二獎", 7), ("三獎", 6), ("四獎", 5), ("五獎", 4), ("六獎", 3)]
_AMOUNT = dict(PRIZES)


# ---- 期別與日期 ---------------------------------------------------------------

def period_of(day: date) -> str:
    """發票期別，例如 2026-07-15 → "11508"（民國 115 年 7–8 月期）。"""
    even = day.month if day.month % 2 == 0 else day.month + 1
    return f"{day.year - 1911}{even:02d}"


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def period_label(period: str) -> str:
    end = int(period[-2:])
    return f"{period[:-2]} 年 {end - 1}–{end} 月"


def draw_date(period: str) -> date:
    roc, end = int(period[:-2]), int(period[-2:])
    y, m = _add_months(roc + 1911, end, 1)
    return date(y, m, 25)


def claim_window(period: str) -> tuple[date, date]:
    roc, end = int(period[:-2]), int(period[-2:])
    sy, sm = _add_months(roc + 1911, end, 2)
    ey, em = _add_months(roc + 1911, end, 5)
    return date(sy, sm, 6), date(ey, em, 5)


# ---- 解析官方頁面 ---------------------------------------------------------------

@dataclass
class Draw:
    period: str
    special: str
    grand: str
    first: list[str]
    extra_sixth: list[str]
    source: str = "official"

    def check(self, inv_num: str) -> tuple[str, int] | None:
        """回傳最高的獎項 (名稱, 獎金)，沒中回傳 None。只看發票號碼的 8 位數字。"""
        digits = re.sub(r"\D", "", inv_num or "")[-8:]
        if len(digits) != 8:
            return None
        if digits == self.special:
            return "特別獎", _AMOUNT["特別獎"]
        if digits == self.grand:
            return "特獎", _AMOUNT["特獎"]
        for name, k in _FIRST_TIERS:
            if any(digits[-k:] == f[-k:] for f in self.first):
                return name, _AMOUNT[name]
        if any(digits[-3:] == x[-3:] for x in self.extra_sixth):
            return "增開六獎", 200
        return None


_PERIOD_ON = re.compile(r'class="etw-on"[^>]*title="(\d{3})年(\d{2})-(\d{2})月中獎號碼單"')
_ROW = re.compile(r"<tr>\s*<td[^>]*>\s*(特別獎|特獎|頭獎|增開六獎)\s*</td>(.*?)</tr>", re.S)
_BIG = re.compile(r'<p class="etw-tbiggest[^"]*">(.*?)</p>', re.S)


def parse_page(html: str) -> Draw:
    """解析 index.html / lastNumber.html。版面變了就丟 ValueError，不要猜。"""
    m = _PERIOD_ON.search(html)
    if not m:
        raise ValueError("找不到這一頁的期別（etw-on 的中獎號碼單連結）")
    roc, _start, end = m.groups()
    period = f"{roc}{end}"
    numbers: dict[str, list[str]] = {}
    for name, cell in _ROW.findall(html):
        nums = []
        for p in _BIG.findall(cell):
            digits = re.sub(r"<[^>]+>|\s", "", p)
            if re.fullmatch(r"\d{3}|\d{8}", digits):
                nums.append(digits)
        numbers[name] = nums
    special, grand, first = numbers.get("特別獎", []), numbers.get("特獎", []), numbers.get("頭獎", [])
    if len(special) != 1 or len(grand) != 1 or not first or any(len(n) != 8 for n in [*special, *grand, *first]):
        raise ValueError(f"{period} 期的獎號格式不對：{numbers}")
    return Draw(period, special[0], grand[0], first, numbers.get("增開六獎", []))


def _ssl_context() -> ssl.SSLContext:
    """一樣驗證憑證，只是不開 Python 3.13+ 預設的 X.509 嚴格模式。

    財政部網站的憑證鏈（TWCA）缺 Subject Key Identifier，嚴格模式會拒絕；
    瀏覽器和 curl 都接受這張憑證，所以只關掉這一條，其他檢查（主機名稱、信任鏈、到期）照舊。
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (koukou; +https://github.com/pengwho/koukou)"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLError):
            raise
        # 有些 Python（例如 Homebrew 版）找不到系統的根憑證；改用 curl_cffi 內建的憑證再試一次
        from curl_cffi import requests as creq

        log.info("urllib 驗不過憑證（%s），改用 curl_cffi", e.reason)
        resp = creq.get(url, timeout=20, impersonate="chrome")
        resp.raise_for_status()
        return resp.content.decode("utf-8", "replace")


def fetch_draws(get: Callable[[str], str] = _http_get) -> list[Draw]:
    return [parse_page(get(BASE_URL + page)) for page in PAGES]


# ---- 對獎 -------------------------------------------------------------------------

def award_info(raw_json: str | None) -> str:
    """財政部 App 回傳的中獎標記（雲端發票專屬獎等）。沒有就回傳空字串。"""
    try:
        info = json.loads(raw_json or "{}").get("awardInfo")
    except (ValueError, AttributeError):
        return ""
    if not info:
        return ""
    return info if isinstance(info, str) else json.dumps(info, ensure_ascii=False)


def summarize(invoices: list[dict[str, Any]], draws: dict[str, Draw], today: date) -> list[dict[str, Any]]:
    """依期別整理：開獎了沒、哪幾張中了、什麼時候能領。invoices 需要 inv_num, inv_date, period, raw_json。"""
    by_period: dict[str, list[dict[str, Any]]] = {}
    for inv in invoices:
        if inv.get("inv_date"):
            by_period.setdefault(inv["period"], []).append(inv)
    out = []
    for period in sorted(by_period, reverse=True):
        invs = by_period[period]
        draw = draws.get(period)
        start, end = claim_window(period)
        winners = []
        for inv in invs:
            hit = draw.check(inv["inv_num"]) if draw else None
            cloud = award_info(inv.get("raw_json"))
            if hit or cloud:
                winners.append({
                    "inv_num": inv["inv_num"], "inv_date": inv["inv_date"], "merchant": inv.get("merchant"),
                    "amount": inv.get("amount"), "prize": hit[0] if hit else "財政部標記中獎",
                    "prize_amount": hit[1] if hit else None, "award_info": cloud,
                })
        winners.sort(key=lambda w: -(w["prize_amount"] or 0))
        drawn = today >= draw_date(period)
        if draw:
            status = "drawn"
        elif not drawn:
            status = "pending"
        elif today > end:
            status = "expired"  # 官方頁面只留最近兩期；更早的已經過了領獎期限，查不到也領不到
        else:
            status = "waiting_numbers"
        out.append({
            "period": period, "label": period_label(period), "count": len(invs), "status": status,
            "draw_date": draw_date(period).isoformat(), "claim_start": start.isoformat(),
            "claim_end": end.isoformat(), "claimable": start <= today <= end,
            "expired": today > end, "winners": winners,
            "total": sum(w["prize_amount"] or 0 for w in winners),
            "demo": bool(draw and draw.source == "demo"),
        })
    return out
