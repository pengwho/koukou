"""模擬財政部 App API 的假伺服器：會真的解密登入、驗證 JWT 簽章。"""

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from koukou.sources.einvoice.protocol import ldata_context

SSME = "server-secret"
CARRIER = "/AB12CD3"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeServer:
    """10 月有 2 頁、每頁 2 張發票；每張 2 個品項。expire_on=N 讓第 N 個查詢回 401。"""

    def __init__(self, expire_on: int | None = None) -> None:
        self.calls: list[str] = []
        self.logins = 0
        self.queries = 0
        self.expire_on = expire_on

    def post(self, url: str, headers: dict, body: str):
        if url.endswith("/mid/v1/login"):
            self.logins += 1
            self.calls.append("login")
            a, ct, b = json.loads(body)["ldata"].split("|")
            ctx = ldata_context(a, b)
            inner = json.loads(AESGCM(ctx.key).decrypt(ctx.iv, base64.b64decode(ct), None))
            if (inner["account"], inner["password"]) != ("0912345678", "pw"):
                err = AESGCM(ctx.key).encrypt(ctx.iv, "帳號或密碼錯誤".encode(), None)
                return 200, json.dumps({"result": -151, "payload": base64.b64encode(err).decode()})
            payload = {"sid": "S", "token": "T", "appid": "A", "liat": 1, "ssme": SSME, "carrier_code": CARRIER}
            enc = AESGCM(ctx.key).encrypt(ctx.iv, json.dumps(payload).encode(), None)
            return 200, json.dumps({"result": 0, "payload": base64.b64encode(enc).decode()})

        self.queries += 1
        if self.queries == self.expire_on:
            self.calls.append("401")
            return 200, json.dumps({"code": "401", "msg": "沒有Token或驗證有誤", "version": "1.0"})
        jwt = parse_qs(body)["einvoiceJwt"][0]
        h, c, sig = jwt.split(".")
        expected = hmac.new(SSME.encode(), f"{h}.{c}".encode(), hashlib.sha256).digest()
        assert hmac.compare_digest(_b64d(sig), expected), "JWT 簽章錯誤"
        req = json.loads(_b64d(c))["reqdata"]
        assert req["cardNo"] == CARRIER
        if req["action"] == "carrierInvChk":
            self.calls.append(f"header:{req['startDate'][:7]}:p{req['page']}")
            if not req["startDate"].startswith("2026/10") or req["page"] > 2:
                return 200, json.dumps({"carrierQueryList": {"code": "200", "msg": "ok", "details": []}})
            n = req["page"]
            rows = [{"invNum": f"AB1234567{n}{i}", "sellerName": "全家便利商店股份有限公司台北市第一分公司",
                     "amount": "125",
                     "invDate": {"year": "115", "month": "10", "date": str(n + i), "hours": "9",
                                 "minutes": "5", "seconds": "0"}}
                    for i in range(2)]
            return 200, json.dumps({"carrierQueryList": {"code": "200", "msg": "ok", "details": rows}})
        self.calls.append("detail")
        assert req["invDate"].startswith("2026/10/")
        return 200, json.dumps({"code": "200", "msg": "ok", "details": [
            {"rowNum": "1", "description": "鮮奶", "quantity": "1", "unitPrice": "85", "amount": "85"},
            {"rowNum": "2", "description": "麵包", "quantity": "1", "unitPrice": "40", "amount": "40"}]})

    def close(self) -> None:
        pass
