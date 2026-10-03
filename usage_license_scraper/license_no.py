"""解析使用執照字號：年、字、號。

輸入檔的字號寫法很不統一，系統上的寫法也跟輸入不完全一樣（補零位數、有沒有「字第」），
所以比對時不直接比字串，而是拆成（年、字、號）三段各自正規化後再比：

    '(83)桃縣工建使字第其00203號' -> 年 083、字 桃縣工建使其、號 203
    '83重使1639號'               -> 年 083、字 重使、號 1639
    '88年桃縣工建使字第壢1653號'  -> 年 088、字 桃縣工建使壢、號 1653
    '97桃縣工建使字第蘆2077號等'  -> 年 097、字 桃縣工建使蘆、號 2077
    '115中都使字第00001號'        -> 年 115、字 中都使、號 1
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from usage_license_scraper.normalize import normalize_license_text

_PATTERN = re.compile(r"^\(?(\d{2,3})\)?年?(.*?)(\d+)號?$")


@dataclass(frozen=True)
class LicenseNo:
    year: str    # 3 碼，例如 '083'
    word: str    # 正規化後的「字」，去掉「字」「第」，例如 '桃縣工建使其'
    number: str  # 去掉前導 0，例如 '203'


def parse_license_no(text: str) -> LicenseNo | None:
    m = _PATTERN.match(normalize_license_text(text))
    if not m:
        return None
    year, word, number = m.groups()
    return LicenseNo(year=year.zfill(3), word=re.sub(r"[字第]", "", word), number=str(int(number)))
