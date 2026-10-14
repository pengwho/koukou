"""發送 HTTP 請求的兩種方式。

財政部主機前面有 Cloudflare：一般的 requests / httpx 會因 TLS 指紋被擋（403）。
- CurlTransport：用 curl_cffi 模仿 Chrome 的 TLS 指紋，最輕量，先試這個。
- BrowserTransport：開真的 headless Chromium，在頁面裡 fetch。curl 被擋時改用它。

另外，查詢主機會拒絕帶 Origin 標頭的請求（原生 App 不會帶），
瀏覽器模式下要在網路層把 Origin / Referer 拔掉。
"""

from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import urlsplit


class Transport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: str) -> tuple[int, str]: ...
    def close(self) -> None: ...


class CurlTransport:
    def __init__(self) -> None:
        from curl_cffi import requests as creq

        self._session = creq.Session(impersonate="chrome")

    def post(self, url: str, headers: dict[str, str], body: str) -> tuple[int, str]:
        r = self._session.post(url, headers=headers, data=body.encode("utf-8"), timeout=30)
        return r.status_code, r.text

    def close(self) -> None:
        self._session.close()


class BrowserTransport:
    def __init__(self, headless: bool = True) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        major = self._browser.version.split(".")[0]
        # 預設 UA 含 "HeadlessChrome"，會被拒，換成一般 Chrome 的 UA
        ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )
        self._context = self._browser.new_context(user_agent=ua)
        self._page = self._context.new_page()
        self._page.route(re.compile(r"^https://[^/]*einvoice\.nat\.gov\.tw/"), self._strip_origin)
        self._origin = ""

    @staticmethod
    def _strip_origin(route, request) -> None:
        headers = {
            k: v
            for k, v in request.all_headers().items()
            if k.lower() not in ("origin", "referer")
        }
        route.continue_(headers=headers)

    def _ensure_origin(self, url: str) -> None:
        # fetch 要在同網域頁面裡發，否則會被 CORS 擋；先切到該主機
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin != self._origin:
            try:
                self._page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass  # 首頁本身 404 也沒關係，只要 origin 對了就好
            self._origin = origin

    def post(self, url: str, headers: dict[str, str], body: str) -> tuple[int, str]:
        self._ensure_origin(url)
        result = self._page.evaluate(
            """async ({url, headers, body}) => {
                try {
                    const r = await fetch(url, {method: "POST", headers, body, referrerPolicy: "no-referrer"});
                    return {status: r.status, text: await r.text()};
                } catch (e) {
                    return {status: 0, text: String(e)};
                }
            }""",
            {"url": url, "headers": headers, "body": body},
        )
        return result["status"], result["text"]

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._pw.stop()


def make_transport(kind: str, headless: bool = True) -> Transport:
    if kind == "curl":
        return CurlTransport()
    if kind == "browser":
        return BrowserTransport(headless=headless)
    raise ValueError(f"未知的 transport：{kind}")
