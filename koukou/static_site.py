"""把展示帳本匯出成純靜態網站，放上 GitHub Pages 就能線上試玩。

畫面跟本機版是同一份 index.html / app.js；app.js 看到 koukou-mode=static 時，
改從 data/ledger.json 讀資料、在瀏覽器裡做篩選與搜尋，不需要任何伺服器。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

from . import __version__
from .demo import build_demo_db
from .web.app import STATIC


def ledger_json(store, demo: bool = True) -> dict:
    months = store.months()
    data = {
        "meta": {"version": __version__, "demo": demo, "static": True, "months": months,
                 "default_month": months[-1] if months else None},
        "monthly": store.monthly(),
        "months": {},
    }
    for m in months:
        invoices = store.invoices(m, limit=10_000)
        for inv in invoices:
            inv["items"] = store.items(inv["inv_num"])
        data["months"][m] = {
            "summary": {k: v for k, v in store.summary(m).items() if k != "last_sync"},
            "merchants": store.merchants(m, limit=50),
            "invoices": invoices,
        }
    return data


def build(out: Path, today: date | None = None) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(STATIC, out / "static")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    marker = '<meta charset="utf-8">'
    assert marker in html
    html = html.replace(marker, marker + '\n  <meta name="koukou-mode" content="static">', 1)
    (out / "index.html").write_text(html, encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        store = build_demo_db(Path(tmp) / "demo.sqlite3", today=today)
        payload = ledger_json(store)
        store.close()
    (out / "data").mkdir()
    (out / "data" / "ledger.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out / ".nojekyll").write_text("")
    return out
