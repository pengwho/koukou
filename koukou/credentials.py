"""手機條碼帳密的存放：本次執行的記憶體 → macOS 鑰匙圈 → 環境變數 / .env。

勾選「記住在這台電腦」時存進系統鑰匙圈（macOS Keychain、Windows 認證管理員、
Linux Secret Service）；沒有可用的鑰匙圈時只留在記憶體，關掉就忘記。
帳密不會寫進任何檔案，也不會出現在 log。
"""

from __future__ import annotations

import logging

from . import config

SERVICE = "koukou.einvoice"
log = logging.getLogger("koukou.credentials")

_session: dict[str, str] = {}


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:  # noqa: BLE001 — 沒裝或載入失敗都當作沒有鑰匙圈
        return None


def load() -> tuple[str, str] | None:
    if _session.get("phone") and _session.get("password"):
        return _session["phone"], _session["password"]
    kr = _keyring()
    if kr is not None:
        try:
            phone = kr.get_password(SERVICE, "phone")
            password = kr.get_password(SERVICE, phone) if phone else None
            if phone and password:
                return phone, password
        except Exception as e:  # noqa: BLE001
            log.debug("keyring unavailable: %s", type(e).__name__)
    phone, password = config.env_value("EINVOICE_PHONE"), config.env_value("EINVOICE_PASSWORD")
    if phone and password:
        return phone, password
    return None


def save(phone: str, password: str, remember: bool) -> bool:
    """回傳是否成功存進鑰匙圈。不管有沒有存成功，本次執行都會記住。"""
    _session.update(phone=phone, password=password)
    if not remember:
        return False
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, "phone", phone)
        kr.set_password(SERVICE, phone, password)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("keyring save failed: %s", type(e).__name__)
        return False


def clear() -> None:
    phone = _session.get("phone")
    _session.clear()
    kr = _keyring()
    if kr is None:
        return
    try:
        phone = phone or kr.get_password(SERVICE, "phone")
        if phone:
            kr.delete_password(SERVICE, phone)
        kr.delete_password(SERVICE, "phone")
    except Exception as e:  # noqa: BLE001
        log.debug("keyring clear failed: %s", type(e).__name__)


# ---- 其他秘密（例如 AI 金鑰）：只存鑰匙圈，沒有鑰匙圈時只留在記憶體 ----

_secrets: dict[str, str] = {}


def get_secret(name: str) -> str:
    if name in _secrets:
        return _secrets[name]
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return kr.get_password("koukou", name) or ""
    except Exception as e:  # noqa: BLE001
        log.debug("keyring get failed: %s", type(e).__name__)
        return ""


def set_secret(name: str, value: str) -> bool:
    _secrets[name] = value
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password("koukou", name, value)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("keyring set failed: %s", type(e).__name__)
        return False
