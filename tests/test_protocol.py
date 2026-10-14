"""協定加密層：對照 TypeScript 參考實作產生的固定向量，逐位元比對。

向量由 OctopusBeak src/workflows/einvoice-app-protocol.ts（協定源自 all-set-tw, MIT）產生，
固定在這裡讓 CI 不需要 Node 或外部原始碼。
"""

from koukou.sources.einvoice.protocol import (
    Session,
    decrypt_login_data,
    encrypt_login_data,
    ldata_context,
    sign_invoice_jwt,
)

REF_KEY = "4833b31601c1bcfec76d0c3bb5c7d338e5ef33fc88786e075301554f93df1166"
REF_IV = "4b6c4d6e4f70303132333435"
REF_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsInZlciI6IjEuMCJ9."
    "eyJjb21tcyI6eyJpYXQiOjE3NjAwMDAwMDAsImV4cCI6MTc2MDAwMDA0MCwibGlhdCI6MTcwMDAwMDAwMCwiYXBwaWQiOiJhcHAxMjMiLCJi"
    "YXJjb2RlIjoiL0FCQysxMjMiLCJrZXlUeXBlIjoiMiJ9LCJyZXFkYXRhIjp7InZlcnNpb24iOiIxLjAiLCJhY3Rpb24iOiJjYXJyaWVySW52"
    "Q2hrIiwic3RhcnREYXRlIjoiMjAyNi8xMC8wMSIsInBhZ2UiOjF9fQ."
    "8xgQUcWIO-HtwqlRwqq4Es0k2wLHMSQ9R2aFkohDI0k"
)
REF_TS_LDATA = (
    "Wu31tW9QsKt72yf3|tTyKk8Lj6aPSIsoJROzzsunvotZEpTL2/P2B/DfukIdD5nU5A40l20rlYXR8nOguPBD6r7t49S7WNX7hDw0o6P8="
    "|XKP49A6xTnFgJMwL"
)
PLAIN = {"type": 0, "account": "0912345678", "note": "中文"}


def test_key_derivation_matches_reference():
    ctx = ldata_context("AbCdEfGhIjKlMnOp", "QrStUvWxYz012345")
    assert ctx.key.hex() == REF_KEY
    assert ctx.iv.hex() == REF_IV


def test_jwt_matches_reference():
    session = Session(sid="x", token="x", appid="app123", liat=1700000000, ssme="s3cr3t-ssme",
                      carrier_code="/ABC+123")
    request = {"version": "1.0", "action": "carrierInvChk", "startDate": "2026/10/01", "page": 1}
    assert sign_invoice_jwt(request, session, 1760000000) == REF_JWT


def test_decrypts_reference_ciphertext():
    assert decrypt_login_data(REF_TS_LDATA) == PLAIN


def test_encrypt_roundtrip():
    ldata, ctx = encrypt_login_data(PLAIN)
    assert decrypt_login_data(ldata) == PLAIN
    # 伺服器回傳的是不帶種子的純 base64，要用請求時的 context 解
    assert decrypt_login_data(ldata.split("|")[1], ctx) == PLAIN
