"""新竹縣使用執照查詢（build.hsinchu.gov.tw）。

目前暫時沿用 opendata 標準端點（見 adapters/opendata/），所以查詢方式跟
桃園市等縣市一樣。但已實測發現新竹縣這份開放資料**只更新到民國 95～96 年
左右**，之後的年度（含 115 年）查不到資料——這是縣市端資料沒有更新到最新，
不是程式邏輯錯誤；查近期年度的執照會正常落在 errors.csv（查無資料）。

之後要改走其他查詢來源時：
1. 改繼承 `CityAdapter`（不要再繼承 OpenDataLicenseAdapter），實作 `fetch()`。
2. 依新來源能支援的查詢方式，覆寫 `fetch_year()`／`search_by_address()`／
   `search_by_land()`，並設定 `supported_queries`。
3. 需要人工驗證碼的話，設 `requires_captcha = True` 並實作 `open_session()`
   （可參考 adapters/tainan/）。
4. HTML／JSON 解析建議另外放在同資料夾的 parser.py。
"""

from __future__ import annotations

from usage_license_scraper.adapters.base import register
from usage_license_scraper.adapters.opendata.adapter import OpenDataLicenseAdapter


@register("新竹縣")
class HsinchuCountyAdapter(OpenDataLicenseAdapter):
    city_name = "新竹縣"
    base_url = "https://build.hsinchu.gov.tw"
