"""台北市：開放資料集（XML）匯入本機索引後查詢，每月自動下載當年度資料。

- parser.py  XML（新舊兩種格式）→ LicenseRecord
- index.py   本機索引的建立、自動下載與查詢
- adapter.py 台北市 adapter
"""

from usage_license_scraper.adapters.taipei.adapter import TaipeiAdapter

__all__ = ["TaipeiAdapter"]
