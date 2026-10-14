"""財政部「統一發票兌獎」手機 App v2 協定的加密原語。

這支模組只做純運算（加解密、簽章），不發任何網路請求。
協定細節參考 TedLin1993/all-set-tw（MIT）與 WangWilly/OctopusBeak 的實作。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MIDDLE_HOST = "https://uia.einvoice.nat.gov.tw"  # 登入
BIG_HOST = "https://upi.einvoice.nat.gov.tw"  # 查詢發票
APP_VERSION = "6.800.2"  # 官方 App 改版後若被拒，通常改這裡
APP_BUILD = 66

_ALNUM = string.ascii_uppercase + string.ascii_lowercase + string.digits


def _random_alnum(length: int) -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(length))


def random_client_code() -> str:
    """每次登入時 App 產生的 8 碼 ckey。"""
    return _random_alnum(8)


def _swap_pairs(value: str) -> str:
    out = []
    for i in range(0, len(value), 2):
        if i + 1 < len(value):
            out.append(value[i + 1])
        out.append(value[i])
    return "".join(out)


def _compact_json(data: Any) -> str:
    # 等同 JavaScript 的 JSON.stringify：無空白、不跳脫中文
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class LDataContext:
    key: bytes
    iv: bytes


def ldata_context(a: str, b: str) -> LDataContext:
    """由兩段 16 碼亂數種子推導 AES-256-GCM 的 key 與 iv。"""
    seed = _swap_pairs(b) + _swap_pairs(a) + a[::-1] + b[::-1]
    return LDataContext(
        key=hashlib.sha256(seed.encode("utf-8")).digest(),
        iv=(a[-6:] + b[-6:]).encode("utf-8"),
    )


def encrypt_login_data(data: dict[str, Any]) -> tuple[str, LDataContext]:
    """登入請求的 ldata 信封：`a|base64(密文+tag)|b`。"""
    a, b = _random_alnum(16), _random_alnum(16)
    ctx = ldata_context(a, b)
    ciphertext = AESGCM(ctx.key).encrypt(ctx.iv, _compact_json(data).encode("utf-8"), None)
    return f"{a}|{base64.b64encode(ciphertext).decode()}|{b}", ctx


def decrypt_login_data(value: str, request_ctx: LDataContext | None = None) -> Any:
    """解開登入回應。伺服器通常回傳單純 base64，用請求時的 context 解密。"""
    parts = value.split("|")
    if len(parts) == 3 and len(parts[0]) == 16 and len(parts[2]) == 16:
        ctx, encoded = ldata_context(parts[0], parts[2]), parts[1]
    elif len(parts) == 1 and request_ctx is not None:
        ctx, encoded = request_ctx, value
    else:
        raise ValueError("登入回應的 ldata 格式無法辨識")
    plaintext = AESGCM(ctx.key).decrypt(ctx.iv, base64.b64decode(encoded), None).decode("utf-8")
    try:
        return json.loads(plaintext)
    except json.JSONDecodeError:
        return plaintext  # 錯誤回應有時是純文字


@dataclass
class Session:
    sid: str
    token: str
    appid: str
    liat: int
    ssme: str
    carrier_code: str = ""
    now: float | None = None  # 伺服器時間（秒），用來校正時鐘差

    @classmethod
    def from_login_payload(cls, data: dict[str, Any]) -> Session:
        s = cls(
            sid=str(data.get("sid") or ""),
            token=str(data.get("token") or ""),
            appid=str(data.get("appid") or data.get("loginAppId") or ""),
            liat=int(data.get("liat") or data.get("loginLiat") or 0),
            ssme=str(data.get("ssme") or data.get("loginSsMe") or ""),
            carrier_code=str(data.get("carrier_code") or data.get("carrierCode") or ""),
            now=float(data["now"]) if data.get("now") is not None else None,
        )
        if not (s.sid and s.token and s.appid and s.ssme):
            raise ValueError(f"登入回應缺少 session 欄位，收到的欄位：{sorted(data)}")
        return s

    def server_offset(self, now_ms: float) -> int:
        if self.now is None:
            return 0
        return int(self.now - now_ms / 1000)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_invoice_jwt(request: dict[str, Any], session: Session, now: int) -> str:
    """查詢請求用的 HS256 JWT，密鑰是登入拿到的 ssme。"""
    header = _b64url(_compact_json({"alg": "HS256", "typ": "JWT", "ver": "1.0"}).encode())
    claims = _b64url(
        _compact_json(
            {
                "comms": {
                    "iat": now,
                    "exp": now + 40,
                    "liat": session.liat,
                    "appid": session.appid,
                    "barcode": session.carrier_code,
                    "keyType": "2",
                },
                "reqdata": request,
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}"
    sig = hmac.new(session.ssme.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"
