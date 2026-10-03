"""從地址字串推出行政區（不含縣市），例如「新北市三芝區錫板里…」-> 「三芝區」。

批次輸入有「行政區」欄位，但用 license／year／address／land 查詢時沒有，
各縣市 parser 用這個從門牌補上，讓所有縣市的 district 格式一致。
"""

from __future__ import annotations

import re

# 結尾多吃一個「區鄉鎮市」，避免「平鎮市」「新市區」被切成「平鎮」「新市」
_PATTERN = re.compile(r"^(?:\S{2}[市縣])?(\S+?[區鄉鎮市][區鄉鎮市]?)")


def district_from_address(address: str) -> str:
    m = _PATTERN.match(re.sub(r"^\d{3,6}(?=\D)", "", address.strip()))  # 去掉開頭的郵遞區號
    return m.group(1) if m else ""
