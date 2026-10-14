"""阿財的分類表與規則層。

分類流程是一個 cascade：規則 → 快取（同品名只問一次）→ LLM。
這個檔案負責前兩層用得到的東西：分類定義、品名正規化、關鍵字規則。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: str
    hint: str  # 給 LLM 的定義，也是 UI 的說明


CATEGORIES: tuple[Category, ...] = (
    Category("餐飲", "正餐、便當、套餐、餐廳、速食、外送平台訂單、熟食"),
    Category("咖啡飲料", "現做咖啡、手搖飲、茶飲、瓶裝或罐裝飲料、汽水、果汁（不含鮮奶）"),
    Category("零食點心", "零食、餅乾、糖果、甜點、麵包店糕點、冰淇淋、霜淇淋、御飯糰以外的小點心"),
    Category("生鮮雜貨", "買回家煮或吃的食材：蔬果、肉、蛋、海鮮、鮮奶與乳品、米、吐司、調味料、冷凍食品、"
                       "便利商店的御飯糰與茶葉蛋也算這類"),
    Category("日用品", "衛生紙、清潔用品、洗衣精、洗碗精、牙刷牙膏、沐浴乳、洗髮精、垃圾袋、購物袋、電池"),
    Category("美妝保養", "化妝品、保養品、面膜、洗面乳、化妝棉、香水"),
    Category("交通", "加油、停車、高鐵、台鐵、捷運、客運、計程車、共享機車、悠遊卡加值"),
    Category("3C家電", "手機與配件、充電線、耳機、電腦周邊、家電"),
    Category("居家家具", "家具、收納、廚具、餐具、寢具、燈具、居家裝飾、五金工具"),
    Category("服飾", "衣服、鞋子、包包、襪子、配件"),
    Category("醫療保健", "藥品、保健食品、口罩、醫療用品、診所掛號"),
    Category("娛樂", "電影、遊戲、書籍、訂閱服務、展覽、運動場館"),
    Category("運費手續費", "運費、手續費、服務費、會員費"),
    Category("折扣", "折扣、折抵、優惠、點數折抵等負數項目"),
    Category("其他", "以上都不適合時才用"),
)
CATEGORY_IDS = tuple(c.id for c in CATEGORIES)
UNCATEGORIZED = "未分類"


def normalize(description: str | None) -> str:
    """快取用的品名鍵：全形轉半形、去頭尾空白、壓縮空白、英文轉小寫。"""
    text = unicodedata.normalize("NFKC", description or "")
    return re.sub(r"\s+", " ", text).strip().lower()


# (正規表示式, 分類)。只放幾乎不會錯的規則；模糊的交給 LLM。
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), category)
    for pattern, category in (
        (r"運費|手續費|服務費", "運費手續費"),
        (r"無鉛汽油|柴油|加油|停車|高鐵|台鐵|悠遊卡加值|一卡通加值", "交通"),
        (r"購物袋|塑膠袋|環保袋", "日用品"),
        (r"外送訂單|便當|套餐", "餐飲"),
        # 「美式」單獨出現太危險（美式雞腿堡、美式辣起司玉米片），只認「美式咖啡」和「冰／熱美式」
        (r"咖啡(?!豆|凍|粉|機|膠囊|因)|拿鐵|那堤|瑪奇朵|美式咖啡|[冰熱]美式|latte|americano", "咖啡飲料"),
        (r"衛生紙|面紙|洗碗精|洗衣精|洗衣球", "日用品"),
    )
)


_DISCOUNT = re.compile(r"折扣|折抵|折價|優惠|點數|discount")


def rule_category(description: str | None, amount: float | None = None) -> str | None:
    """規則能判斷就回傳分類，否則 None。負數品項一律是折扣。"""
    if amount is not None and amount < 0:
        return "折扣"
    key = normalize(description)
    if not key:
        return None
    # 「優惠組」之類的正價商品不能當成折扣，所以折扣關鍵字只在金額不是正數時才算
    if (amount is None or amount <= 0) and _DISCOUNT.search(key):
        return "折扣"
    for pattern, category in _RULES:
        if pattern.search(key):
            return category
    return None
