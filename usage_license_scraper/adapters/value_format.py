"""明細頁欄位值的格式對齊（台南 NBUPIC、新竹縣 bupic 共用），目標格式同 opendata 縣市。"""

from __future__ import annotations

import re


def clean_value(value: str) -> str:
    """去掉單位前的空格（'112.39 ㎡' -> '112.39㎡'）；系統隱藏的 '***'、'* * *' 當成空字串。"""
    v = re.sub(r"\s+(?=[㎡％ｍ%])", "", value.strip())
    return "" if set(v.replace(" ", "")) <= {"*"} else v


def number_with_unit(value: str, unit: str = "") -> str:
    """'3.2000' -> '3.2ｍ'；'64.7200' -> '64.72㎡'；'0.0000' -> '0㎡'。不是數字就原樣回傳。"""
    v = value.strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", v):
        return v
    if "." in v:
        v = v.rstrip("0").rstrip(".")
    return f"{v}{unit}"


def normalize_household(value: str) -> str:
    """'地上4層地下1層 1幢 2棟 30戶' -> '1幢2棟30戶，地上4層 地下1層'"""
    t = value.replace(" ", "")

    def get(pattern: str) -> str:
        m = re.search(pattern, t)
        return m.group(1) if m else ""

    above, below = get(r"地上(\d+)層"), get(r"地下(\d+)層")
    zhuang, dong, hu = get(r"(\d+)幢"), get(r"(\d+)棟"), get(r"(\d+)戶")
    if not (above or dong or hu):
        return value
    head = (f"{zhuang}幢" if zhuang else "") + (f"{dong}棟" if dong else "") + (f"{hu}戶" if hu else "")
    return f"{head}，地上{above or 0}層 地下{below or 0}層"


def normalize_cost(value: str) -> str:
    """'新台幣壹佰肆拾萬零玖仟元整（$1,409,000）' -> '1409000'"""
    m = re.search(r"\$\s*([\d,]+)", value)
    if m:
        return m.group(1).replace(",", "")
    v = value.replace(",", "").removesuffix("元").strip()
    return v if v.isdigit() else clean_value(value)
