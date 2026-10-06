"""各縣市 adapter，依查詢系統分組：

- opendata/        桃園市／新竹市／台中市／高雄市（全國建管系統 opendata API）
- new_taipei/      新北市（公開明細頁，只能用使用執照號查）
- hsinchu_county/  新竹縣（opendata ＋ bupic 明細頁，後者需使用者提供瀏覽器工作階段）
- tainan/          台南市（NBUPIC，需人工驗證碼）
- taipei/          台北市（開放資料集匯入本機索引，每月自動更新）
"""

from usage_license_scraper.adapters.base import CityAdapter, LicenseNotFound, QueryType, cities_supporting, registry

# 匯入各組套件，觸發 @register 裝飾器完成註冊
from usage_license_scraper.adapters import opendata  # noqa: E402,F401
from usage_license_scraper.adapters import new_taipei  # noqa: E402,F401
from usage_license_scraper.adapters import hsinchu_county  # noqa: E402,F401
from usage_license_scraper.adapters import tainan  # noqa: E402,F401
from usage_license_scraper.adapters import taipei  # noqa: E402,F401

__all__ = ["CityAdapter", "LicenseNotFound", "QueryType", "cities_supporting", "registry"]
