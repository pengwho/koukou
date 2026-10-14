"""資料目錄、.env 讀取、裝置 ID。

預設資料放在 ~/.koukou/，可用環境變數 KOUKOU_HOME 改位置。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def home() -> Path:
    path = Path(os.environ.get("KOUKOU_HOME", Path.home() / ".koukou")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db() -> Path:
    return home() / "koukou.sqlite3"


def demo_db() -> Path:
    return home() / "demo.sqlite3"


def load_dotenv() -> None:
    """依序讀取目前目錄與 ~/.koukou 的 .env（不覆蓋已存在的環境變數）。"""
    for path in (Path.cwd() / ".env", home() / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_value(name: str) -> str:
    """讀環境變數；空白或仍是 .env.example 的範例值都當作沒填。"""
    value = os.environ.get(name, "").strip()
    return "" if not value or "xxxx" in value else value


def device_id() -> str:
    """固定的裝置 ID，模仿同一支手機重複登入。"""
    path = home() / ".device_id"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_hex(8)
    path.write_text(value)
    return value
