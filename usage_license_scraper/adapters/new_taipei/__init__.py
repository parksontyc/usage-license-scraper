"""新北市：公開明細頁 bm_detail.jsp，只支援用使用執照號碼查詢。

- adapter.py 組明細頁 URL、抓取
- parser.py  明細頁 HTML → LicenseRecord
"""

from usage_license_scraper.adapters.new_taipei.adapter import NewTaipeiAdapter

__all__ = ["NewTaipeiAdapter"]
