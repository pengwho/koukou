"""展示模式：產生一份逼真但完全虛構的發票帳本，不用帳密也能看到完整介面。

資料以 App 回傳的原始格式產生，再走跟真實資料一樣的 Store.save()，
所以展示模式同時也是整條寫入路徑的測試。
"""

from __future__ import annotations

import random
import zlib
from datetime import date, timedelta
from pathlib import Path

from .store import Store

# (法定名稱, 出現權重, 每張品項數範圍, 品項目錄 [(品名, 單價)])
MERCHANTS = [
    ("統一超商股份有限公司台北市第七三一分公司", 22, (1, 3), [
        ("城市咖啡 大杯拿鐵", 65), ("御飯糰 鮪魚", 35), ("茶葉蛋", 13), ("光泉鮮乳 290ml", 32),
        ("義美小泡芙", 25), ("大亨堡", 49), ("統一陽光豆漿", 25), ("關東煮", 40),
        ("咖波 聯名 杯套", 99), ("咖波 聯名 貼紙", 59)]),
    ("全家便利商店股份有限公司台北市第三五二分公司", 18, (1, 3), [
        ("Let's Café 冰美式", 45), ("烤地瓜", 39), ("全家 炙燒雞腿便當", 89), ("雙層鮮奶油泡芙", 39),
        ("FamiCollection 蘇打餅", 29), ("霜淇淋", 39), ("集點 兌換 好禮", 59), ("集點 兌換 公仔", 49)]),
    ("全聯實業股份有限公司台北萬華分公司", 8, (3, 9), [
        ("台糖 嫩雞胸", 119), ("高麗菜", 59), ("福壽 洗選蛋10入", 79), ("美式賣場 衛生紙", 299),
        ("義美 豆漿 1857ml", 75), ("香蕉", 49), ("台灣鯛魚片", 129), ("洗碗精補充包", 89),
        ("味全 林鳳營鮮乳", 98), ("白米 2.5kg", 189)]),
    ("新加坡商蝦皮娛樂電商有限公司台灣分公司", 6, (1, 2), [
        ("手機殼 透明防摔", 129), ("USB-C 傳輸線 1m", 99), ("收納盒 3入", 239), ("運費", 60),
        ("藍牙耳機 替換耳塞", 89), ("桌上型手機架", 159), ("客製化 刺繡 鑰匙圈", 180)]),
    ("和德昌股份有限公司台北中山分公司", 5, (1, 3), [
        ("大麥克 套餐", 170), ("麥香雞", 59), ("中薯", 55), ("蛋捲冰淇淋", 25), ("可樂 中杯", 38)]),
    ("優比快台灣銷售有限公司", 4, (1, 1), [
        ("Uber Eats 外送訂單", 285), ("Uber Eats 外送訂單", 342), ("Uber Eats 外送訂單", 198)]),
    ("台灣中油股份有限公司台北營業處", 3, (1, 1), [
        ("95 無鉛汽油", 1080), ("95 無鉛汽油", 865), ("98 無鉛汽油", 1240)]),
    ("宜家家居股份有限公司新店分公司", 1, (2, 5), [
        ("KALLAX 層架組", 1499), ("SAMLA 收納盒 22L", 129), ("瑞典肉丸 10入", 159),
        ("FRAKTA 購物袋", 39), ("霜淇淋", 15), ("LACK 邊桌", 299), ("SWEDISH MEATBALLS 12P", 179)]),
    ("家福股份有限公司內湖分公司", 3, (3, 8), [
        ("家樂福 鮮奶", 89), ("日本進口 草莓", 299), ("洗衣精 補充包", 199), ("冷凍水餃 50入", 239),
        ("可口可樂 6入", 135), ("衛生紙 12捲", 259), ("吐司", 45)]),
    ("統一星巴克股份有限公司台北民生分公司", 4, (1, 2), [
        ("那堤 大杯", 140), ("焦糖瑪奇朵 大杯", 160), ("美式咖啡 中杯", 105), ("巧克力可頌", 75)]),
    ("康達盛通生活事業股份有限公司台北復興分公司", 3, (1, 2), [
        ("OK 精選 咖啡", 45), ("御飯糰 鮭魚", 35), ("茶裏王 綠茶", 25)]),
    ("鼎泰豐小吃店股份有限公司信義分公司", 1, (3, 5), [
        ("小籠包 10個", 250), ("蝦仁蛋炒飯", 240), ("排骨蛋炒飯", 260), ("酸辣湯 小", 95), ("紅油抄手", 160)]),
    ("台灣高速鐵路股份有限公司", 1, (1, 1), [("高鐵 台北-左營 標準車廂", 1490), ("高鐵 台北-台中 標準車廂", 700)]),
    ("寶雅國際股份有限公司台北南京分公司", 2, (2, 4), [
        ("洗面乳", 199), ("化妝棉", 69), ("牙刷 2入", 89), ("沐浴乳 補充包", 159), ("面膜 5片", 199)]),
]

# 展示資料的品項是人工標好的分類（來源標成 demo，不假裝是 AI 分的）
DEMO_LABELS = {
    '電信月租費 4G 599 型': '其他',
    '會員月費': '娛樂',
    '電費': '其他',
    'Uber Eats 外送訂單': '餐飲',
    '中薯': '餐飲',
    '全家 炙燒雞腿便當': '餐飲',
    '大亨堡': '餐飲',
    '大麥克 套餐': '餐飲',
    '小籠包 10個': '餐飲',
    '排骨蛋炒飯': '餐飲',
    '瑞典肉丸 10入': '餐飲',
    '紅油抄手': '餐飲',
    '蝦仁蛋炒飯': '餐飲',
    '酸辣湯 小': '餐飲',
    '關東煮': '餐飲',
    '麥香雞': '餐飲',
    "Let's Café 冰美式": '咖啡飲料',
    'OK 精選 咖啡': '咖啡飲料',
    '可口可樂 6入': '咖啡飲料',
    '可樂 中杯': '咖啡飲料',
    '城市咖啡 大杯拿鐵': '咖啡飲料',
    '焦糖瑪奇朵 大杯': '咖啡飲料',
    '統一陽光豆漿': '咖啡飲料',
    '美式咖啡 中杯': '咖啡飲料',
    '義美 豆漿 1857ml': '咖啡飲料',
    '茶裏王 綠茶': '咖啡飲料',
    '那堤 大杯': '咖啡飲料',
    'FamiCollection 蘇打餅': '零食點心',
    '巧克力可頌': '零食點心',
    '烤地瓜': '零食點心',
    '義美小泡芙': '零食點心',
    '蛋捲冰淇淋': '零食點心',
    '雙層鮮奶油泡芙': '零食點心',
    '霜淇淋': '零食點心',
    '光泉鮮乳 290ml': '生鮮雜貨',
    '冷凍水餃 50入': '生鮮雜貨',
    '台灣鯛魚片': '生鮮雜貨',
    '台糖 嫩雞胸': '生鮮雜貨',
    '吐司': '生鮮雜貨',
    '味全 林鳳營鮮乳': '生鮮雜貨',
    '家樂福 鮮奶': '生鮮雜貨',
    '御飯糰 鮪魚': '生鮮雜貨',
    '御飯糰 鮭魚': '生鮮雜貨',
    '日本進口 草莓': '生鮮雜貨',
    '白米 2.5kg': '生鮮雜貨',
    '福壽 洗選蛋10入': '生鮮雜貨',
    '茶葉蛋': '生鮮雜貨',
    '香蕉': '生鮮雜貨',
    '高麗菜': '生鮮雜貨',
    'FRAKTA 購物袋': '日用品',
    '沐浴乳 補充包': '日用品',
    '洗碗精補充包': '日用品',
    '洗衣精 補充包': '日用品',
    '牙刷 2入': '日用品',
    '美式賣場 衛生紙': '日用品',
    '衛生紙 12捲': '日用品',
    '化妝棉': '美妝保養',
    '洗面乳': '美妝保養',
    '面膜 5片': '美妝保養',
    '95 無鉛汽油': '交通',
    '98 無鉛汽油': '交通',
    '高鐵 台北-台中 標準車廂': '交通',
    '高鐵 台北-左營 標準車廂': '交通',
    'USB-C 傳輸線 1m': '3C家電',
    '手機殼 透明防摔': '3C家電',
    '桌上型手機架': '3C家電',
    '藍牙耳機 替換耳塞': '3C家電',
    'KALLAX 層架組': '居家家具',
    'LACK 邊桌': '居家家具',
    'SAMLA 收納盒 22L': '居家家具',
    '收納盒 3入': '居家家具',
    '運費': '運費手續費',
    '促銷折扣': '折扣',
}

_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def _period(d: date) -> str:
    """發票期別：民國年 + 雙月，例如 115 年 9-10 月 → 11510。"""
    even = d.month if d.month % 2 == 0 else d.month + 1
    return f"{d.year - 1911}{even:02d}"


def generate(months: int = 8, today: date | None = None, seed: int = 20261014):
    """產生 (header, items) 清單，格式與 App API 回傳相同。"""
    rng = random.Random(seed)
    today = today or date.today()
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    start = date(y, m, 1)
    weights = [m[1] for m in MERCHANTS]
    out = []
    day = start
    mood: dict[tuple[int, int], float] = {}  # 每個月的花錢程度不一樣
    while day <= today:
        factor = mood.setdefault((day.year, day.month), rng.uniform(0.55, 1.5))
        # 平日 0–3 張，週末多一點
        n = rng.choices([0, 1, 2, 3], weights=[2, 4, 3, 2] if day.weekday() < 5 else [1, 3, 4, 3])[0]
        n = round(n * factor)
        for _ in range(n):
            seller, _, (lo, hi), catalog = rng.choices(MERCHANTS, weights=weights)[0]
            picks = [rng.choice(catalog) for _ in range(rng.randint(lo, hi))]
            items, total = [], 0
            for i, (name, price) in enumerate(picks, 1):
                qty = 1 if price > 150 else rng.choice([1, 1, 1, 2])
                items.append({"rowNum": str(i), "description": name, "quantity": str(qty),
                              "unitPrice": str(price), "amount": str(price * qty)})
                total += price * qty
            if rng.random() < 0.08 and total > 100:  # 偶爾有促銷折扣
                disc = -rng.choice([10, 20, 30])
                items.append({"rowNum": str(len(items) + 1), "description": "促銷折扣", "quantity": "1",
                              "unitPrice": str(disc), "amount": str(disc)})
                total += disc
            hh, mm, ss = rng.randint(7, 22), rng.randint(0, 59), rng.randint(0, 59)
            header = {
                "invNum": f"{rng.choice(_LETTERS)}{rng.choice(_LETTERS)}{rng.randint(0, 99999999):08d}",
                "invPeriod": _period(day),
                "invDate": {"year": str(day.year - 1911), "month": str(day.month), "date": str(day.day),
                            "hours": str(hh), "minutes": str(mm), "seconds": str(ss)},
                "invoiceTime": f"{hh:02d}:{mm:02d}:{ss:02d}",
                "sellerName": seller,
                "sellerBan": f"{zlib.crc32(seller.encode()) % 10**8:08d}",
                "amount": str(total),
                "invStatus": "開立已確認",
                "cardType": "3J0002",
                "cardNo": "/DEMO123",
                "demo": True,
            }
            out.append((header, items))
        day += timedelta(days=1)
    out += _bills(start, today)
    return out


# 固定支出：每月的電信、健身房，兩個月一次的電費（金額會浮動）。用獨立的亂數，不影響上面的消費
_BILLS = [
    # (店, 品名, 每幾個月, 幾號, 金額範圍)
    ("台灣大哥大股份有限公司", "電信月租費 4G 599 型", 1, 5, (599, 599)),
    ("健身工廠股份有限公司台北分公司", "會員月費", 1, 1, (1288, 1288)),
    ("台灣電力股份有限公司台北市區營業處", "電費", 2, 20, (880, 1650)),
]


def _bills(start: date, today: date) -> list[tuple[dict, list[dict]]]:
    rng = random.Random(7)
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= today:
        for seller, desc, every, day_no, (lo, hi) in _BILLS:
            d = date(y, m, day_no)
            if m % every or d > today:
                continue
            amount = rng.randint(lo, hi)
            out.append(({
                "invNum": f"{rng.choice(_LETTERS)}{rng.choice(_LETTERS)}{rng.randint(0, 99999999):08d}",
                "invPeriod": _period(d),
                "invDate": {"year": str(d.year - 1911), "month": str(d.month), "date": str(d.day),
                            "hours": "9", "minutes": "0", "seconds": "0"},
                "invoiceTime": "09:00:00", "sellerName": seller,
                "sellerBan": f"{zlib.crc32(seller.encode()) % 10**8:08d}", "amount": str(amount),
                "invStatus": "開立已確認", "cardType": "3J0002", "cardNo": "/DEMO123", "demo": True,
            }, [{"rowNum": "1", "description": desc, "quantity": "1", "unitPrice": str(amount),
                 "amount": str(amount)}]))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def demo_draws(store: Store, today: date) -> None:
    """展示用中獎號碼（不是真的號碼）：最近兩期各讓一兩張發票中小獎，展示對獎的樣子。"""
    from .lottery import Draw, draw_date

    by_period: dict[str, list[str]] = {}
    for inv in store.lottery_invoices():
        by_period.setdefault(inv["period"], []).append(inv["inv_num"])
    drawn = sorted((p for p in by_period if draw_date(p) <= today), reverse=True)[:2]
    for i, period in enumerate(drawn):
        rng = random.Random(f"draw-{period}")
        nums = sorted(by_period[period])
        a = nums[len(nums) // 3][-3:]          # 這張中六獎（末 3 碼）
        first = [f"{rng.randint(0, 99999):05d}{a}"]
        if i == 0:
            b = nums[2 * len(nums) // 3][-4:]  # 最近一期再多一張中五獎（末 4 碼）
            first.append(f"{rng.randint(0, 9999):04d}{b}")
        while len(first) < 3:
            first.append(f"{rng.randint(0, 99999999):08d}")
        store.save_draw(Draw(period, f"{rng.randint(0, 99999999):08d}", f"{rng.randint(0, 99999999):08d}",
                             first, [], "demo"))


def build_demo_db(path: Path, months: int = 8, today: date | None = None) -> Store:
    if path.exists():
        path.unlink()
    store = Store(path)
    for header, items in generate(months, today):
        store.save(header, items)
    from .ai.categories import normalize

    # 大部分品名用人工標註；刻意留幾個給內建小模型猜，展示「阿財沒把握時會問你」
    left_for_model = {
        "FamiCollection 蘇打餅", "SAMLA 收納盒 22L", "瑞典肉丸 10入", "面膜 5片", "統一陽光豆漿", "桌上型手機架",
        "酸辣湯 小", "美式賣場 衛生紙", "LACK 邊桌", "OK 精選 咖啡", "中薯", "雙層鮮奶油泡芙",
    }
    store.save_categories([(normalize(n), n, c, "demo", 1.0, "")
                           for n, c in DEMO_LABELS.items() if n not in left_for_model])
    from .ai.categorizer import categorize_store

    categorize_store(store, None)
    demo_draws(store, today or date.today())
    run = store.start_run("demo")
    store.finish_run(run, "ok", store.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], "展示資料")
    return store
