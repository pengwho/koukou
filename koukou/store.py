"""本機 SQLite 帳本：寫入發票、查詢儀表板需要的彙總、匯出 CSV。

重複寫入同一張發票會以發票號碼覆寫，不會重複。
"""

from __future__ import annotations

import csv
import functools
import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .ai.categories import UNCATEGORIZED, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    inv_num      TEXT PRIMARY KEY,
    inv_date     TEXT,          -- YYYY-MM-DD（西元）
    inv_time     TEXT,          -- HH:MM:SS
    seller_name  TEXT,
    seller_ban   TEXT,          -- 賣方統編
    amount       REAL,
    inv_status   TEXT,
    card_type    TEXT,
    card_no      TEXT,
    raw_json     TEXT,
    fetched_at   TEXT
);
CREATE TABLE IF NOT EXISTS items (
    inv_num      TEXT,
    row_num      INTEGER,
    description  TEXT,
    quantity     REAL,
    unit_price   REAL,
    amount       REAL,
    PRIMARY KEY (inv_num, row_num)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,          -- running / ok / error
    invoices     INTEGER,
    message      TEXT
);
CREATE INDEX IF NOT EXISTS invoices_date ON invoices (inv_date);
-- 阿財的分類快取：以正規化後的品名為鍵，同名品項共用一個分類
CREATE TABLE IF NOT EXISTS item_categories (
    key          TEXT PRIMARY KEY,
    description  TEXT,
    category     TEXT,
    source       TEXT,          -- rule / llm / user / demo
    confidence   REAL,
    model        TEXT,
    updated_at   TEXT
);
"""

# 外商在台分公司的前綴，例如「新加坡商蝦皮娛樂電商有限公司台灣分公司」
_FOREIGN_PREFIX = re.compile(
    r"^(英屬維京群島|英屬蓋曼群島|開曼群島|薩摩亞|新加坡|香港|美|英|日|德|法|荷蘭|瑞士|韓)商"
)
_COMPANY_SUFFIX = re.compile(r"(股份)?有限公司.*$|企業社.*$|商行.*$")


# 法定名稱跟平常講的店名差很多的，直接對照
_BRANDS = {
    "統一超商": "7-ELEVEN",
    "全家便利商店": "全家",
    "全聯實業": "全聯",
    "萊爾富國際": "萊爾富",
    "康達盛通生活事業": "OK 超商",
    "宜家家居": "IKEA",
    "蝦皮娛樂電商": "蝦皮",
    "酷澎": "酷澎 Coupang",
    "優比快台灣銷售": "Uber Eats",
    "和德昌": "麥當勞",
    "家福": "家樂福",
    "統一星巴克": "星巴克",
    "遠百企業": "遠東百貨",
    "台灣中油": "中油",
    "富邦媒體科技": "momo",
    "鼎泰豐小吃店": "鼎泰豐",
    "台灣高速鐵路": "台灣高鐵",
    "寶雅國際": "寶雅",
}


def merchant_name(seller_name: str | None) -> str:
    """把法定名稱縮成平常講的店名：「全聯實業股份有限公司中壢青埔分公司」→「全聯」。"""
    if not seller_name:
        return "（未知商家）"
    name = _FOREIGN_PREFIX.sub("", seller_name.strip())
    short = _COMPANY_SUFFIX.sub("", name) or name
    return _BRANDS.get(short, short)


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def header_date(header: dict[str, Any]) -> tuple[str | None, str | None]:
    """App 回傳的 invDate 是物件，year 可能是民國年（如 "115"）。"""
    d = header.get("invDate") or {}
    try:
        y, m, day = int(d["year"]), int(d["month"]), int(d["date"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if y < 1911:
        y += 1911
    date = f"{y:04d}-{m:02d}-{day:02d}"
    t = header.get("invoiceTime")
    if not t and all(k in d for k in ("hours", "minutes", "seconds")):
        t = f"{int(d['hours']):02d}:{int(d['minutes']):02d}:{int(d['seconds']):02d}"
    return date, t


def _locked(method):
    """同一個 Store 可能被網頁的多個請求執行緒同時使用；一次只讓一個執行緒碰連線。
    （共用連線加上 Python 自訂函式 koukou_norm，不加鎖會互相卡死。）"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # 背景同步寫入時，畫面照樣能讀
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.create_function("koukou_norm", 1, normalize, deterministic=True)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- 寫入 -------------------------------------------------------------

    @_locked
    def save(self, header: dict[str, Any], items: list[dict[str, Any]] | None) -> None:
        inv_num = header.get("invNum")
        if not inv_num:
            return
        date, time_ = header_date(header)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    inv_num, date, time_, header.get("sellerName"), header.get("sellerBan"),
                    _num(header.get("amount")), header.get("invStatus"), header.get("cardType"),
                    header.get("cardNo"), json.dumps(header, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            if items is not None:
                self.db.execute("DELETE FROM items WHERE inv_num = ?", (inv_num,))
                self.db.executemany(
                    "INSERT INTO items VALUES (?,?,?,?,?,?)",
                    [
                        (
                            inv_num, int(_num(it.get("rowNum")) or i + 1), it.get("description"),
                            _num(it.get("quantity")), _num(it.get("unitPrice")), _num(it.get("amount")),
                        )
                        for i, it in enumerate(items)
                    ],
                )

    @_locked
    def has_items(self, inv_num: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE inv_num = ? LIMIT 1", (inv_num,)).fetchone() is not None

    @_locked
    def start_run(self, source: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO sync_runs (source, started_at, status) VALUES (?, ?, 'running')",
                (source, datetime.now().isoformat(timespec="seconds")),
            )
        return int(cur.lastrowid)

    @_locked
    def finish_run(self, run_id: int, status: str, invoices: int, message: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, invoices=?, message=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), status, invoices, message, run_id),
            )

    # ---- 儀表板查詢 ---------------------------------------------------------

    @_locked
    def months(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT substr(inv_date,1,7) m FROM invoices WHERE inv_date IS NOT NULL ORDER BY m"
        )
        return [r["m"] for r in rows]

    @_locked
    def monthly(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT substr(inv_date,1,7) AS month, COUNT(*) AS count, ROUND(SUM(amount)) AS amount "
            "FROM invoices WHERE inv_date IS NOT NULL GROUP BY 1 ORDER BY 1"
        )
        return [dict(r) for r in rows]

    @_locked
    def summary(self, month: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT COUNT(*) AS count, COALESCE(ROUND(SUM(amount)),0) AS amount, "
            "COUNT(DISTINCT seller_ban) AS merchants FROM invoices WHERE inv_date LIKE ?",
            (f"{month}%",),
        ).fetchone()
        result = dict(row)
        result["month"] = month
        result["average"] = round(result["amount"] / result["count"]) if result["count"] else 0
        last = self.db.execute(
            "SELECT finished_at, status, message FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        result["last_sync"] = dict(last) if last else None
        return result

    @_locked
    def merchants(self, month: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        where, args = ("WHERE inv_date LIKE ?", [f"{month}%"]) if month else ("", [])
        rows = self.db.execute(
            f"SELECT seller_name, amount FROM invoices {where}", args
        ).fetchall()
        totals: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = merchant_name(r["seller_name"])
            t = totals.setdefault(key, {"name": key, "count": 0, "amount": 0.0})
            t["count"] += 1
            t["amount"] += r["amount"] or 0
        ranked = sorted(totals.values(), key=lambda t: t["amount"], reverse=True)
        for t in ranked:
            t["amount"] = round(t["amount"])
        return ranked[:limit]

    @_locked
    def invoices(
        self, month: str | None = None, q: str = "", limit: int = 200, offset: int = 0,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, args = [], []
        if category:
            match = (
                "EXISTS (SELECT 1 FROM items it LEFT JOIN item_categories c ON c.key = koukou_norm(it.description) "
                "WHERE it.inv_num = i.inv_num AND it.amount > 0 AND COALESCE(c.category, ?) = ?)"
            )
            if category == UNCATEGORIZED:
                match = f"({match} OR NOT EXISTS (SELECT 1 FROM items it WHERE it.inv_num = i.inv_num))"
            clauses.append(match)
            args += [UNCATEGORIZED, category]
        if month:
            clauses.append("i.inv_date LIKE ?")
            args.append(f"{month}%")
        if q:
            clauses.append(
                "(i.seller_name LIKE ? OR i.inv_num LIKE ? OR EXISTS "
                "(SELECT 1 FROM items it WHERE it.inv_num = i.inv_num AND it.description LIKE ?))"
            )
            args += [f"%{q}%"] * 3
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            f"SELECT i.inv_num, i.inv_date, i.inv_time, i.seller_name, i.amount, "
            f"(SELECT COUNT(*) FROM items it WHERE it.inv_num = i.inv_num) AS item_count "
            f"FROM invoices i {where} ORDER BY i.inv_date DESC, i.inv_time DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        out = []
        for r in rows:
            d = dict(r)
            d["merchant"] = merchant_name(d["seller_name"])
            out.append(d)
        return out

    @_locked
    def items(self, inv_num: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT it.row_num, it.description, it.quantity, it.unit_price, it.amount, "
            "c.category, c.source AS category_source, c.confidence "
            "FROM items it LEFT JOIN item_categories c ON c.key = koukou_norm(it.description) "
            "WHERE it.inv_num = ? ORDER BY it.row_num",
            (inv_num,),
        )
        return [dict(r) for r in rows]

    # ---- 阿財的分類 ---------------------------------------------------------

    @_locked
    def pending_items(self, include_local: bool = False) -> list[tuple[str, str, str, float]]:
        """還沒分類的品名：(key, 品名, 最常出現的店, 最大金額)。最大金額是負的才代表一定是折扣。
        include_local=True 時，連內建小模型分過的也一起回傳（重算很便宜）。"""
        from collections import Counter

        cond = "(c.key IS NULL OR c.source = 'local')" if include_local else "c.key IS NULL"
        rows = self.db.execute(
            "SELECT it.description, i.seller_name, it.amount FROM items it JOIN invoices i USING (inv_num) "
            "LEFT JOIN item_categories c ON c.key = koukou_norm(it.description) "
            f"WHERE {cond} AND it.description IS NOT NULL AND it.description != ''"
        ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = normalize(r["description"])
            g = groups.setdefault(key, {"desc": r["description"], "shops": Counter(), "max": None})
            g["shops"][merchant_name(r["seller_name"])] += 1
            amt = r["amount"] if r["amount"] is not None else 0
            g["max"] = amt if g["max"] is None else max(g["max"], amt)
        return [(k, g["desc"], g["shops"].most_common(1)[0][0], g["max"]) for k, g in groups.items()]

    @_locked
    def save_categories(self, rows: list[tuple[str, str, str, str, float, str]]) -> None:
        """rows: (key, 品名, 分類, 來源, 信心, 模型)。使用者改過的不會被覆蓋。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self.db:
            self.db.executemany(
                "INSERT INTO item_categories VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "category=excluded.category, source=excluded.source, confidence=excluded.confidence, "
                "model=excluded.model, updated_at=excluded.updated_at WHERE item_categories.source != 'user'",
                [(*r, now) for r in rows],
            )

    @_locked
    def set_user_category(self, description: str, category: str) -> int:
        """使用者手動改分類；回傳同名品項有幾筆會一起套用。"""
        key = normalize(description)
        now = datetime.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "INSERT INTO item_categories VALUES (?,?,?,'user',1.0,'',?) ON CONFLICT(key) DO UPDATE SET "
                "category=excluded.category, source='user', confidence=1.0, model='', updated_at=excluded.updated_at",
                (key, description, category, now),
            )
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE koukou_norm(description) = ?", (key,)
        ).fetchone()[0]

    @_locked
    def user_examples(self, limit: int = 30) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT description, category FROM item_categories WHERE source = 'user' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,),
        )
        return [(r["description"], r["category"]) for r in rows]

    @_locked
    @_locked
    def confident_llm_labels(self, min_confidence: float = 0.8) -> list[tuple[str, str]]:
        """LLM 有把握的答案，拿來當內建小模型的額外訓練資料（本機蒸餾）。"""
        rows = self.db.execute(
            "SELECT description, category FROM item_categories WHERE source = 'llm' AND confidence >= ?",
            (min_confidence,),
        )
        return [(r["description"], r["category"]) for r in rows]

    def category_stats(self) -> dict[str, int]:
        stats = {"rule": 0, "local": 0, "llm": 0, "user": 0, "demo": 0, "low_confidence": 0}
        for r in self.db.execute("SELECT source, COUNT(*) n FROM item_categories GROUP BY source"):
            stats[r["source"]] = r["n"]
        stats["low_confidence"] = self.db.execute(
            "SELECT COUNT(*) FROM item_categories WHERE source IN ('llm', 'local') AND confidence < 0.6"
        ).fetchone()[0]
        stats["pending"] = len(self.pending_items())
        return stats

    @_locked
    def breakdown(self, month: str) -> list[dict[str, Any]]:
        """當月各分類花多少。折扣、點數折抵和發票差額依比例攤回同一張發票的其他品項，
        所以各分類加總會等於當月發票總額。"""
        rows = self.db.execute(
            "SELECT i.inv_num, i.amount AS inv_amount, it.amount, c.category FROM invoices i "
            "LEFT JOIN items it USING (inv_num) "
            "LEFT JOIN item_categories c ON c.key = koukou_norm(it.description) "
            "WHERE i.inv_date LIKE ?", (f"{month}%",),
        ).fetchall()
        per_invoice: dict[str, dict[str, Any]] = {}
        for r in rows:
            inv = per_invoice.setdefault(r["inv_num"], {"total": r["inv_amount"] or 0, "parts": []})
            if r["amount"] is not None and r["amount"] > 0 and r["category"] != "折扣":
                inv["parts"].append((r["category"] or UNCATEGORIZED, r["amount"]))
        totals: dict[str, float] = {}
        counts: dict[str, set[str]] = {}
        for inv_num, inv in per_invoice.items():
            parts = inv["parts"] or [(UNCATEGORIZED, inv["total"])]
            base = sum(a for _, a in parts) or 1
            for cat, amt in parts:
                totals[cat] = totals.get(cat, 0) + amt * inv["total"] / base
                counts.setdefault(cat, set()).add(inv_num)
        grand = sum(totals.values()) or 1
        out = [
            {"category": cat, "amount": round(amt), "share": amt / grand, "invoices": len(counts[cat])}
            for cat, amt in totals.items()
        ]
        return sorted(out, key=lambda d: d["amount"], reverse=True)

    # ---- 匯出 ---------------------------------------------------------------

    @_locked
    def export_csv(self, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, sql in (
            ("invoices.csv", "SELECT inv_num, inv_date, inv_time, seller_name, seller_ban, amount, inv_status "
                             "FROM invoices ORDER BY inv_date, inv_time"),
            ("items.csv", "SELECT i.inv_date, it.inv_num, i.seller_name, it.row_num, it.description, "
                          "it.quantity, it.unit_price, it.amount FROM items it "
                          "JOIN invoices i USING (inv_num) ORDER BY i.inv_date, it.inv_num, it.row_num"),
        ):
            cur = self.db.execute(sql)
            p = out_dir / name
            with p.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig 讓 Excel 正確顯示中文
                w = csv.writer(f)
                w.writerow([c[0] for c in cur.description])
                w.writerows(tuple(r) for r in cur)
            paths.append(p)
        return paths
