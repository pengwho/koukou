"""命令列入口。

    koukou demo                 # 用虛構資料打開介面，不需要帳密
    koukou serve                # 打開你自己的帳本
    koukou fetch                # 從命令列同步最近 6 個月的發票
    koukou export               # 匯出 CSV
"""

from __future__ import annotations

import argparse
import getpass
import logging
import re
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__, config, credentials
from .store import Store


def _serve(db: Path, demo: bool, port: int, open_browser: bool) -> int:
    import uvicorn

    from .web.app import create_app

    url = f"http://127.0.0.1:{port}/"
    print(f"摳摳開在 {url}（按 Ctrl+C 結束）")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(db, demo=demo), host="127.0.0.1", port=port, log_level="warning")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import build_demo_db

    db = config.demo_db()
    store = build_demo_db(db)
    count = store.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    store.close()
    print(f"已產生 {count} 張虛構發票。")
    return _serve(db, True, args.port, not args.no_browser)


def cmd_serve(args: argparse.Namespace) -> int:
    config.load_dotenv()
    return _serve(args.db, False, args.port, not args.no_browser)


def cmd_fetch(args: argparse.Namespace) -> int:
    from .sources.einvoice.client import BlockedByCloudflare
    from .sources.einvoice.sync import sync_einvoice

    config.load_dotenv()
    log_path = config.home() / "last_run.log"
    logging.basicConfig(filename=log_path, filemode="w", level=logging.DEBUG, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    saved = credentials.load()  # 網頁上連結過的帳密（鑰匙圈）或 .env
    phone = saved[0] if saved else input("手機號碼（09 開頭 10 碼）：").strip()
    if not re.fullmatch(r"09\d{8}", phone):
        print(f"手機號碼格式不對：{phone!r}，請填 09 開頭的 10 位數字", file=sys.stderr)
        return 1
    password = saved[1] if saved else getpass.getpass("手機條碼驗證碼（輸入時不會顯示）：")

    store = Store(args.db)
    run_id = store.start_run("einvoice")
    try:
        result = sync_einvoice(
            store, phone, password, config.device_id(),
            months=args.months, transport=args.transport, headless=not args.headed,
            with_details=not args.no_details, refresh_details=args.refresh_details,
            delay=args.delay, progress=lambda step: print(f"  {step}…"),
        )
    except BlockedByCloudflare as e:
        store.finish_run(run_id, "error", 0, str(e))
        logging.exception("blocked")
        print(f"錯誤：{e}（詳細紀錄：{log_path}）", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        store.finish_run(run_id, "error", 0, str(e))
        logging.exception("failed")
        print(f"錯誤：{e}（詳細紀錄：{log_path}）", file=sys.stderr)
        return 1
    store.finish_run(run_id, "ok", result.invoices)

    for month, count in result.per_month.items():
        print(f"  {month}：{count} 張")
    extra = f"，途中自動重新登入 {result.relogins} 次" if result.relogins else ""
    print(f"完成，共 {result.invoices} 張{extra}。帳本：{args.db}")
    print("用 koukou serve 打開介面。")
    return 0


def cmd_build_static(args: argparse.Namespace) -> int:
    from .static_site import build

    out = build(args.out)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"已輸出靜態展示網站到 {out}（{size / 1024:.0f} KB）")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    for path in Store(args.db).export_csv(args.out):
        print(f"已匯出 {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="koukou", description="摳摳 KouKou — 把你的摳摳一筆一筆摳回來")
    parser.add_argument("--version", action="version", version=f"koukou {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def web_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, default=8765)
        p.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")

    p = sub.add_parser("demo", help="用虛構資料打開介面")
    web_flags(p)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="打開你自己的帳本")
    p.add_argument("--db", type=Path, default=config.default_db())
    web_flags(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("fetch", help="同步手機條碼發票")
    p.add_argument("--db", type=Path, default=config.default_db())
    p.add_argument("--months", type=int, default=6, help="往回抓幾個月（含本月），預設 6")
    p.add_argument("--transport", choices=["curl", "browser"], default="curl",
                   help="curl 被 Cloudflare 擋下時改用 browser（需安裝 playwright）")
    p.add_argument("--headed", action="store_true", help="browser 模式顯示視窗")
    p.add_argument("--no-details", action="store_true", help="只抓表頭，不抓品項")
    p.add_argument("--refresh-details", action="store_true", help="已抓過的品項也重抓")
    p.add_argument("--delay", type=float, default=0.3, help="每次查詢間隔秒數")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("build-static", help="輸出可放上 GitHub Pages 的線上展示")
    p.add_argument("--out", type=Path, default=Path("site"))
    p.set_defaults(func=cmd_build_static)

    p = sub.add_parser("export", help="匯出 invoices.csv 與 items.csv")
    p.add_argument("--db", type=Path, default=config.default_db())
    p.add_argument("--out", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
