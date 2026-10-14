"""本機網頁介面：FastAPI 提供 JSON API，靜態頁面負責畫面。只綁 127.0.0.1。"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, config
from ..store import Store

STATIC = Path(__file__).parent / "static"


class SyncJob:
    """一次只跑一個同步；網頁輪詢 /api/sync 取得進度。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False, "step": "", "error": "", "result": None}

    def start(self, db_path: Path, months: int) -> None:
        phone, password = config.env_value("EINVOICE_PHONE"), config.env_value("EINVOICE_PASSWORD")
        if not phone or not password:
            raise HTTPException(
                400,
                "還沒設定手機條碼帳密。請在 ~/.koukou/.env 填入 EINVOICE_PHONE 和 EINVOICE_PASSWORD，再重新啟動。",
            )
        with self.lock:
            if self.state["running"]:
                raise HTTPException(409, "正在同步中，請稍候")
            self.state = {"running": True, "step": "準備中", "error": "", "result": None}
        threading.Thread(target=self._run, args=(db_path, phone, password, months), daemon=True).start()

    def _run(self, db_path: Path, phone: str, password: str, months: int) -> None:
        from ..sources.einvoice.sync import sync_einvoice

        store = Store(db_path)
        run_id = store.start_run("einvoice")
        try:
            result = sync_einvoice(
                store, phone, password, config.device_id(), months=months,
                progress=lambda step: self.state.update(step=step),
            )
            store.finish_run(run_id, "ok", result.invoices)
            self.state.update(result={"invoices": result.invoices, "new_details": result.details_fetched})
        except Exception as e:  # noqa: BLE001 — 任何錯誤都要回報給畫面
            store.finish_run(run_id, "error", 0, str(e))
            self.state.update(error=str(e))
        finally:
            store.close()
            self.state.update(running=False, step="")


def create_app(db_path: Path, demo: bool = False, sync_months: int = 2) -> FastAPI:
    app = FastAPI(title="摳摳 KouKou", version=__version__, docs_url=None, redoc_url=None)
    store = Store(db_path)
    job = SyncJob()

    def default_month() -> str:
        months = store.months()
        return months[-1] if months else date.today().strftime("%Y-%m")

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {"version": __version__, "demo": demo, "months": store.months(), "default_month": default_month()}

    @app.get("/api/summary")
    def summary(month: str | None = None) -> dict[str, Any]:
        return store.summary(month or default_month())

    @app.get("/api/monthly")
    def monthly() -> list[dict[str, Any]]:
        return store.monthly()

    @app.get("/api/merchants")
    def merchants(month: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        return store.merchants(month, limit=min(limit, 50))

    @app.get("/api/invoices")
    def invoices(month: str | None = None, q: str = "", limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return store.invoices(month, q.strip(), limit=min(limit, 500), offset=offset)

    @app.get("/api/invoices/{inv_num}/items")
    def items(inv_num: str) -> list[dict[str, Any]]:
        return store.items(inv_num)

    @app.get("/api/sync")
    def sync_status() -> dict[str, Any]:
        return job.state

    @app.post("/api/sync")
    def sync_start() -> dict[str, Any]:
        if demo:
            raise HTTPException(400, "展示模式不會連線。用 koukou serve（不加 --demo）看你自己的發票。")
        job.start(db_path, sync_months)
        return job.state

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
