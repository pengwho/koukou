"""登入與查詢：把 protocol（加密）和 transport（送出）接起來。"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlencode

from .protocol import (
    APP_BUILD,
    APP_VERSION,
    BIG_HOST,
    MIDDLE_HOST,
    Session,
    decrypt_login_data,
    encrypt_login_data,
    random_client_code,
    sign_invoice_jwt,
)
from .transport import Transport


class EInvoiceError(RuntimeError):
    pass


class BlockedByCloudflare(EInvoiceError):
    pass


class LoginRejected(EInvoiceError):
    """帳號或驗證碼錯誤、帳號被鎖等，伺服器明確拒絕登入。"""


class ProtocolOutdated(EInvoiceError):
    """伺服器不認得我們送的格式，通常是官方 App 改版了。"""


_APP_HEADERS = {
    "Accept": "application/json",
    "appver": APP_VERSION,
    "appbn": str(APP_BUILD),
    "platform": "android",
    "version": APP_VERSION,
}
_JSON_HEADERS = {**_APP_HEADERS, "Content-Type": "application/json; charset=utf-8"}
_FORM_HEADERS = {**_APP_HEADERS, "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}

log = logging.getLogger("koukou.einvoice")

_CF_CHALLENGE = re.compile(r"<title>Just a moment\.\.\.</title>|/cdn-cgi/challenge-platform/")


class EInvoiceClient:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.relogins = 0
        self._creds: tuple[str, str, str] | None = None

    def _post_json(self, op: str, url: str, headers: dict[str, str], body: str) -> dict[str, Any]:
        status, text = self.t.post(url, headers, body)
        # 不記錄請求內容（含帳密 / JWT）；回應只記前 300 字
        log.debug("%s %s -> HTTP %s %s", op, url.split("/", 3)[-1], status, text[:300].replace("\n", " "))
        if status == 403 and _CF_CHALLENGE.search(text):
            raise BlockedByCloudflare(f"{op}: 被 Cloudflare 擋下，試試 --transport browser")
        if status == 403 and "Invalid CORS request" in text:
            raise EInvoiceError(f"{op}: 請求帶了 Origin 標頭被拒")
        if status != 200:
            raise EInvoiceError(f"{op}: HTTP {status} {text[:200]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProtocolOutdated(f"{op}: 回應不是 JSON：{text[:200]}") from e
        if not isinstance(data, dict):
            raise ProtocolOutdated(f"{op}: 回應格式非預期")
        return data

    # ---- 登入 ---------------------------------------------------------------

    def login(self, phone: str, password: str, device_id: str) -> Session:
        self._creds = (phone, password, device_id)  # 給 session 過期時自動重登用
        return self._login(phone, password, device_id)

    def _relogin(self, session: Session) -> None:
        """session 過期（約 30 秒）時重新登入，就地更新 session。"""
        fresh = self._login(*self._creds)
        session.__dict__.update(fresh.__dict__)
        self.relogins += 1
        log.info("session 過期，已重新登入（第 %d 次）", self.relogins)

    def _login(self, phone: str, password: str, device_id: str) -> Session:
        pdid = f"a:{device_id}"
        inner = {
            "type": 0,
            "account": phone,
            "password": password,
            "carrier_code": "",
            "ckey": random_client_code(),
            "device": {
                "os": "a",
                "os_version": "15",
                "model": "Android",
                "pdid": pdid,
                "ptoken": "",
                "appver": APP_VERSION,
                "appbn": APP_BUILD,
                "lang": "zh",
                "ccs": "tw",
            },
            "ts": int(time.time()),
            "pdid": pdid,
        }
        ldata, ctx = encrypt_login_data(inner)
        payload_json = json.dumps({"ldata": ldata}, separators=(",", ":"))
        body = self._post_json("login", f"{MIDDLE_HOST}/mid/v1/login", _JSON_HEADERS, payload_json)
        if body.get("result") != 0:
            detail = body.get("msg") or body.get("message") or ""
            if isinstance(body.get("payload"), str):
                try:  # 錯誤訊息也是用同一把 key 加密的
                    detail = decrypt_login_data(body["payload"], ctx) or detail
                except Exception:
                    pass
            log.debug("login rejected detail: %s", detail)
            raise LoginRejected(f"登入被拒（result={body.get('result')}，{detail or '帳號或驗證碼錯誤？'}）")
        payload = body.get("payload")
        if not isinstance(payload, str):
            raise ProtocolOutdated("登入成功但沒有 payload")
        try:
            return Session.from_login_payload(decrypt_login_data(payload, ctx))
        except Exception as e:
            raise ProtocolOutdated(f"無法解開登入回應：{e}") from e

    # ---- 查詢 ---------------------------------------------------------------

    def _signed_query(self, op: str, path: str, session: Session, request: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            now = int(time.time()) + session.server_offset(time.time() * 1000) - 10
            jwt = sign_invoice_jwt(request, session, now)
            body = self._post_json(op, f"{BIG_HOST}{path}", _FORM_HEADERS, urlencode({"einvoiceJwt": jwt}))
            lst = body.get("carrierQueryList")
            code = str((lst if isinstance(lst, dict) else body).get("code", ""))
            if code != "401" or attempt == 1 or self._creds is None:
                return body
            self._relogin(session)  # 401 沒有Token或驗證有誤 → 重登後重試一次
        return body

    @staticmethod
    def _require_ok(op: str, envelope: dict[str, Any]) -> None:
        code = str(envelope.get("code", ""))
        if code == "903":  # 參數錯誤
            raise ProtocolOutdated(f"{op}: 伺服器回 903 參數錯誤")
        if code != "200":
            raise EInvoiceError(f"{op}: code={code or '(無)'} msg={envelope.get('msg')}")

    def query_headers(self, session: Session, start: str, end: str, page: int) -> list[dict[str, Any]]:
        """查一頁發票表頭。日期格式 YYYY/MM/DD，page 從 1 開始；空列表代表沒有下一頁。"""
        body = self._signed_query(
            "header",
            "/einvoice/carriers/query-invoices-header",
            session,
            {
                "version": "1.0",
                "cardType": "3J0002",  # 手機條碼
                "cardNo": session.carrier_code,
                "action": "carrierInvChk",
                "startDate": start,
                "endDate": end,
                "onlyWinningInv": "A",
                "page": page,
            },
        )
        lst = body.get("carrierQueryList")
        envelope = lst if isinstance(lst, dict) else body
        self._require_ok("header", envelope)
        details = envelope.get("details")
        return details if isinstance(details, list) else []

    def query_detail(self, session: Session, inv_num: str, inv_date: str) -> list[dict[str, Any]]:
        """查一張發票的品項明細。inv_date 格式 YYYY/MM/DD（西元）。"""
        body = self._signed_query(
            "detail",
            "/einvoice/carriers/query-invoices-details",
            session,
            {
                "version": "1.0",
                "cardType": "3J0002",
                "cardNo": session.carrier_code,
                "action": "carrierInvDetail",
                "invNum": inv_num,
                "invDate": inv_date,
            },
        )
        self._require_ok("detail", body)
        details = body.get("details")
        return details if isinstance(details, list) else []
