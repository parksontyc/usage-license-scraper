"""輸入資料的正規化：讀入輸入檔時統一處理（input_loader.py），之後查詢、比對、
「查過了沒」的判斷都用正規化後的值；原始寫法另外保留，輸出 community_license.csv 時照原樣顯示。

- 縣市：臺→台、舊縣名→現名（桃園縣→桃園市、台北縣→新北市…）、「台中」→「台中市」
- 行政區、地址：全形轉半形、臺→台、去空白、「2段」→「二段」；地址去掉結尾的「等」
- 使用執照字號：全形轉半形、臺→台、去空白、去掉結尾的「等」（年、字、號的拆解見 license_no.py）

`check` 指令（cli.py）會輸出 input_normalized.csv（正規化後的輸入檔，可以直接拿來跑 batch）
與 input_check.csv（解析結果與問題清單）。
"""

from __future__ import annotations

import re

_FULLWIDTH = str.maketrans("０１２３４５６７８９－（）～　", "0123456789-()~ ")
# 地名常見的異體字 -> 統一寫法
_VARIANTS = str.maketrans({"臺": "台", "塩": "鹽"})
_CN_NUM = "一二三四五六七八九十"

# 升格／改制前的舊名 -> 現在的名稱
OLD_CITY_NAMES = {
    "台北縣": "新北市",
    "桃園縣": "桃園市",
    "台中縣": "台中市",
    "台南縣": "台南市",
    "高雄縣": "高雄市",
}

# 只寫地名沒寫「市」也不會混淆的（新竹、嘉義有市也有縣，不猜）
_CITY_WITHOUT_SUFFIX = {"台北", "新北", "桃園", "台中", "台南", "高雄", "基隆"}


def normalize_text(text: str) -> str:
    """全形轉半形、臺→台、去空白、「北深路2段」→「北深路二段」。"""
    t = text.translate(_FULLWIDTH).translate(_VARIANTS).replace(" ", "").strip()
    return re.sub(r"(?<!\d)(10|[1-9])段", lambda m: _CN_NUM[int(m.group(1)) - 1] + "段", t)


def normalize_city(city: str) -> str:
    """'臺中市' -> '台中市'；'桃園縣' -> '桃園市'；'台中' -> '台中市'。"""
    t = normalize_text(city)
    t = OLD_CITY_NAMES.get(t, t)
    if t in _CITY_WITHOUT_SUFFIX:
        t += "市"
    return t


def normalize_address(address: str) -> str:
    """'六福路247巷2弄6號5樓 等' -> '六福路247巷2弄6號5樓'；'東橋十街73號之1' -> '東橋十街73之1號'；
    '704臺南市北區…' -> '台南市北區…'（去掉開頭的郵遞區號）"""
    t = re.sub(r"等$", "", normalize_text(address))
    t = re.sub(r"^\d{3,6}(?=\D)", "", t)
    return re.sub(r"(\d+)號之(\d+)(?!樓)", r"\1之\2號", t)


def normalize_license_text(text: str) -> str:
    """'97桃縣工建使字第蘆2077號等' -> '97桃縣工建使字第蘆2077號'"""
    return re.sub(r"等$", "", normalize_text(text))
