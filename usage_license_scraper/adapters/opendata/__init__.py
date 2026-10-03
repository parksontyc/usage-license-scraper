"""「全國建管系統 opendata」標準端點：桃園市／新竹市／台中市／高雄市。

內政部國土管理署訂的標準開放資料介接格式，各縣市建管系統都有實作
（文件網址固定在 `{host}/opendata/docs/a1.html`），公開 JSON API、
不需要登入或驗證碼。支援的查詢方式：批次／使用執照號、整年度、門牌、地號。

- client.py  HTTP 查詢與查詢條件組裝
- parser.py  JSON → LicenseRecord
- adapter.py 共用的 OpenDataLicenseAdapter
- cities.py  各縣市 base_url 註冊
"""

from usage_license_scraper.adapters.opendata import cities  # noqa: F401  觸發 @register
from usage_license_scraper.adapters.opendata.adapter import OpenDataLicenseAdapter

__all__ = ["OpenDataLicenseAdapter"]
