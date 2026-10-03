"""台南市：NBUPIC 系統，需人工輸入一次驗證碼（Playwright），之後用 requests 批次查。

- adapter.py 驗證碼 session 建立、組 IndexKey 抓明細
- parser.py  明細頁 HTML → LicenseRecord
"""

from usage_license_scraper.adapters.tainan.adapter import TainanAdapter

__all__ = ["TainanAdapter"]
