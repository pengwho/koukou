"""把最近幾個月的手機條碼發票同步進帳本。CLI 和網頁的「摳回最新發票」按鈕共用這裡。"""

from __future__ import annotations

import calendar
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ...store import Store, header_date
from .client import EInvoiceClient, EInvoiceError
from .transport import make_transport

log = logging.getLogger("koukou.einvoice")

MAX_PAGES_PER_MONTH = 100


@dataclass
class SyncResult:
    invoices: int = 0
    details_fetched: int = 0
    relogins: int = 0
    per_month: dict[str, int] = field(default_factory=dict)


def month_ranges(n: int, today: date | None = None) -> list[tuple[str, str, str]]:
    """最近 n 個月（含本月）的 (標籤, 起日, 迄日)，日期格式 YYYY/MM/DD。"""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        last = today.day if (y, m) == (today.year, today.month) else calendar.monthrange(y, m)[1]
        out.append((f"{y}-{m:02d}", f"{y}/{m:02d}/01", f"{y}/{m:02d}/{last:02d}"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def sync_einvoice(
    store: Store,
    phone: str,
    password: str,
    device_id: str,
    *,
    months: int = 6,
    transport: str = "curl",
    headless: bool = True,
    with_details: bool = True,
    refresh_details: bool = False,
    delay: float = 0.3,
    progress: Callable[[str], None] = lambda msg: None,
    client: EInvoiceClient | None = None,
) -> SyncResult:
    """登入、逐月逐頁查表頭、補抓沒抓過的品項，全部寫進 store。"""
    result = SyncResult()
    owns_transport = client is None
    if client is None:
        client = EInvoiceClient(make_transport(transport, headless=headless))
    try:
        progress("登入中")
        session = client.login(phone, password, device_id)
        for label, start, end in month_ranges(months):
            progress(f"讀取 {label}")
            count, page = 0, 1
            while True:
                headers = client.query_headers(session, start, end, page)
                time.sleep(delay)
                if not headers:
                    break
                for h in headers:
                    items = None
                    inv_num = h.get("invNum", "")
                    if with_details and (refresh_details or not store.has_items(inv_num)):
                        d, _ = header_date(h)
                        items = client.query_detail(session, inv_num, (d or "").replace("-", "/"))
                        result.details_fetched += 1
                        time.sleep(delay)
                    store.save(h, items)
                count += len(headers)
                page += 1
                if page > MAX_PAGES_PER_MONTH:
                    raise EInvoiceError("分頁超過 100 頁仍未結束，伺服器可能忽略了 page 參數")
            result.per_month[label] = count
            result.invoices += count
            log.info("%s: %d 張", label, count)
        result.relogins = client.relogins
        return result
    finally:
        if owns_transport:
            client.t.close()
