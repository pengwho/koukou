"""本機網頁介面：FastAPI 提供 JSON API，靜態頁面負責畫面。只綁 127.0.0.1。

本機服務也要防網頁攻擊：任何你打開的網站都能叫瀏覽器對 127.0.0.1 發請求。
- Host 檢查擋 DNS rebinding（惡意網域解析到 127.0.0.1 後讀你的帳本）
- 會改變狀態的請求必須帶 X-KouKou 自訂標頭；跨站請求帶自訂標頭會觸發 CORS 預檢，
  而這裡不回 CORS 標頭，所以別的網站送不出來。
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, config, credentials
from ..ai import llm
from ..ai.categories import CATEGORIES, CATEGORY_IDS, UNCATEGORIZED
from ..store import Store

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
FIRST_SYNC_MONTHS = 6  # 第一次連結：抓財政部保留的大約半年
LATER_SYNC_MONTHS = 2  # 之後：抓最近兩個月就夠，舊的已經在帳本裡


class Login(BaseModel):
    phone: str = Field(pattern=r"^09\d{8}$")
    password: str = Field(min_length=1, max_length=64)
    remember: bool = True


class CategoryFix(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    category: str


class AIJob:
    """阿財分類工作。同步完成後也會自動跑一次，把新品項分好。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False, "step": "", "error": "", "result": None}

    def run(self, db_path: Path, background: bool = True) -> None:
        with self.lock:
            if self.state["running"]:
                raise HTTPException(409, "阿財正在分類中")
            self.state = {"running": True, "step": "準備中", "error": "", "result": None}
        if background:
            threading.Thread(target=self._run, args=(db_path,), daemon=True).start()
        else:
            self._run(db_path)

    def _run(self, db_path: Path) -> None:
        from ..ai.categorizer import categorize_store

        store = Store(db_path)
        try:
            info = llm.from_environment()
            stats = categorize_store(store, info.provider, progress=lambda m: self.state.update(step=m))
            self.state.update(result={"rule": stats.rule, "llm": stats.llm, "failed": stats.failed,
                                      "input_tokens": stats.usage.input_tokens,
                                      "output_tokens": stats.usage.output_tokens})
        except Exception as e:  # noqa: BLE001
            self.state.update(error=str(e))
        finally:
            store.close()
            self.state.update(running=False, step="")


class SyncJob:
    """一次只跑一個同步；網頁輪詢 /api/sync 取得進度。"""

    def __init__(self, ai: AIJob | None = None) -> None:
        self.lock = threading.Lock()
        self.ai = ai
        self.state: dict[str, Any] = {"running": False, "step": "", "error": "", "error_kind": "", "result": None}

    def start(self, db_path: Path, months: int) -> None:
        creds = credentials.load()
        if creds is None:
            raise HTTPException(400, "還沒連結手機條碼。")
        with self.lock:
            if self.state["running"]:
                raise HTTPException(409, "正在同步中，請稍候")
            self.state = {"running": True, "step": "準備中", "error": "", "error_kind": "", "result": None}
        threading.Thread(target=self._run, args=(db_path, *creds, months), daemon=True).start()

    def _run(self, db_path: Path, phone: str, password: str, months: int) -> None:
        from ..sources.einvoice.client import LoginRejected
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
            if self.ai is not None and not self.ai.state["running"]:
                self.state.update(step="阿財分類新品項")
                self.ai.run(db_path, background=False)
        except LoginRejected as e:
            store.finish_run(run_id, "error", 0, str(e))
            self.state.update(error=str(e), error_kind="login")
        except Exception as e:  # noqa: BLE001 — 任何錯誤都要回報給畫面
            store.finish_run(run_id, "error", 0, str(e))
            self.state.update(error=str(e), error_kind="other")
        finally:
            store.close()
            self.state.update(running=False, step="")


def create_app(db_path: Path, demo: bool = False) -> FastAPI:
    app = FastAPI(title="摳摳 KouKou", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(db_path)
    ai_job = AIJob()
    job = SyncJob(ai_job)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "只接受本機連線"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-koukou") != "1":
            return JSONResponse({"detail": "缺少 X-KouKou 標頭"}, status_code=403)
        return await call_next(request)

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
    def invoices(
        month: str | None = None, q: str = "", limit: int = 200, offset: int = 0, category: str | None = None,
    ) -> list[dict[str, Any]]:
        return store.invoices(month, q.strip(), limit=min(limit, 500), offset=offset, category=category or None)

    @app.get("/api/categories")
    def categories() -> list[dict[str, str]]:
        return [{"id": c.id, "hint": c.hint} for c in CATEGORIES]

    @app.get("/api/breakdown")
    def breakdown(month: str | None = None) -> list[dict[str, Any]]:
        return store.breakdown(month or default_month())

    @app.post("/api/items/category")
    def fix_category(fix: CategoryFix) -> dict[str, Any]:
        if demo:
            raise HTTPException(400, "展示模式不能改分類。")
        if fix.category not in CATEGORY_IDS:
            raise HTTPException(422, f"沒有「{fix.category}」這個分類")
        return {"applied_to": store.set_user_category(fix.description, fix.category)}

    @app.get("/api/ai")
    def ai_status() -> dict[str, Any]:
        info = llm.from_environment()
        return {"configured": info.configured, "provider": info.name, "model": info.model, "hint": info.hint,
                "stats": store.category_stats(), "uncategorized_label": UNCATEGORIZED, **ai_job.state}

    @app.post("/api/ai/categorize")
    def ai_run() -> dict[str, Any]:
        if demo:
            raise HTTPException(400, "展示資料已經分類好了。")
        ai_job.run(db_path)
        return ai_job.state

    @app.get("/api/invoices/{inv_num}/items")
    def items(inv_num: str) -> list[dict[str, Any]]:
        return store.items(inv_num)

    @app.get("/api/credentials")
    def credentials_status() -> dict[str, Any]:
        creds = None if demo else credentials.load()
        return {"configured": creds is not None, "phone": f"{creds[0][:4]}…{creds[0][-3:]}" if creds else None}

    @app.post("/api/credentials")
    def credentials_save(login: Login) -> dict[str, Any]:
        if demo:
            raise HTTPException(400, "展示模式不會連線。")
        stored = credentials.save(login.phone, login.password, login.remember)
        return {"configured": True, "remembered": stored}

    @app.delete("/api/credentials")
    def credentials_clear() -> dict[str, Any]:
        credentials.clear()
        return {"configured": credentials.load() is not None}

    @app.get("/api/sync")
    def sync_status() -> dict[str, Any]:
        return job.state

    @app.post("/api/sync")
    def sync_start() -> dict[str, Any]:
        if demo:
            raise HTTPException(400, "展示模式不會連線。用 koukou serve（不加 --demo）看你自己的發票。")
        job.start(db_path, FIRST_SYNC_MONTHS if not store.months() else LATER_SYNC_MONTHS)
        return job.state

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
