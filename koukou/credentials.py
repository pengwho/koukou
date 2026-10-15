"""手機條碼帳密的存放：本次執行的記憶體 → macOS 鑰匙圈 → 環境變數 / .env。

勾選「記住在這台電腦」時存進系統鑰匙圈（macOS Keychain、Windows 認證管理員、
Linux Secret Service）；沒有可用的鑰匙圈時只留在記憶體，關掉就忘記。
帳密不會寫進任何檔案，也不會出現在 log。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from . import config

SERVICE = "koukou.einvoice"
log = logging.getLogger("koukou.credentials")

_session: dict[str, str] = {}

# macOS 第一次（或 Python 升級後）讀鑰匙圈會跳出授權視窗，使用者按下去之前呼叫會一直卡著。
# 視窗常被其他視窗蓋住，卡住的話整個伺服器（連 Ctrl+C）都會停住。
# 所以鑰匙圈一律放在背景執行緒、最多等幾秒；還在等的時候回報 waiting，讓畫面提醒使用者去按。
KEYCHAIN_TIMEOUT = 4.0
_pending: threading.Thread | None = None
_pending_lock = threading.Lock()


class KeychainWaiting(Exception):
    """鑰匙圈在等使用者在授權視窗按「允許」。"""


def keychain_waiting() -> bool:
    return _pending is not None and _pending.is_alive()


def _keychain(fn: Callable[[Any], Any]) -> Any:
    """在背景執行 fn(keyring)，超過 KEYCHAIN_TIMEOUT 秒丟 KeychainWaiting；沒有鑰匙圈回傳 None。"""
    global _pending
    kr = _keyring()
    if kr is None:
        return None
    with _pending_lock:
        if keychain_waiting():  # 上一個還卡在授權視窗，不要再疊一個
            raise KeychainWaiting
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["value"] = fn(kr)
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=work, name="koukou-keychain", daemon=True)
        t.start()
        _pending = t
    t.join(KEYCHAIN_TIMEOUT)
    if t.is_alive():
        raise KeychainWaiting
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:  # noqa: BLE001 — 沒裝或載入失敗都當作沒有鑰匙圈
        return None


def load() -> tuple[str, str] | None:
    if _session.get("phone") and _session.get("password"):
        return _session["phone"], _session["password"]
    def read(kr):
        phone = kr.get_password(SERVICE, "phone")
        return phone, (kr.get_password(SERVICE, phone) if phone else None)

    try:
        phone, password = _keychain(read) or (None, None)
        if phone and password:
            _session.update(phone=phone, password=password)  # 讀到一次就記住，不用每次都問鑰匙圈
            return phone, password
    except KeychainWaiting:
        log.info("waiting for keychain permission")
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

    def write(kr):
        kr.set_password(SERVICE, "phone", phone)
        kr.set_password(SERVICE, phone, password)
        return True

    try:
        return bool(_keychain(write))
    except KeychainWaiting:
        log.info("waiting for keychain permission")
        return False
    except Exception as e:  # noqa: BLE001
        log.debug("keyring save failed: %s", type(e).__name__)
        return False


def clear() -> None:
    phone = _session.get("phone")
    _session.clear()

    def delete(kr):
        p = phone or kr.get_password(SERVICE, "phone")
        if p:
            kr.delete_password(SERVICE, p)
        kr.delete_password(SERVICE, "phone")

    try:
        _keychain(delete)
    except Exception as e:  # noqa: BLE001
        log.debug("keyring clear failed: %s", type(e).__name__)


# ---- 其他秘密（例如 AI 金鑰）：只存鑰匙圈，沒有鑰匙圈時只留在記憶體 ----

_secrets: dict[str, str] = {}


def get_secret(name: str) -> str:
    if name in _secrets:
        return _secrets[name]
    try:
        value = _keychain(lambda kr: kr.get_password("koukou", name)) or ""
    except Exception as e:  # noqa: BLE001
        log.debug("keyring get failed: %s", type(e).__name__)
        return ""
    _secrets[name] = value
    return value


def set_secret(name: str, value: str) -> bool:
    _secrets[name] = value
    try:
        return bool(_keychain(lambda kr: kr.set_password("koukou", name, value) or True))
    except Exception as e:  # noqa: BLE001
        log.debug("keyring set failed: %s", type(e).__name__)
        return False
