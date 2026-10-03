"""新北市使用執照查詢（building-management.publicwork.ntpc.gov.tw）。

查詢頁（bm_query.jsp）送出查詢時有驗證碼，但明細頁 bm_detail.jsp 是可以用
「年度＋執照類別代碼＋流水號＋變更次數」直接組 URL 存取的公開頁面，
不需要登入、不需要 session、也不需要驗證碼（已用 credentials:'omit' 驗證過，
直接 fetch 明細頁一樣能拿到完整資料）。因此只支援用使用執照號碼查詢。

URL 參數：
    ri1 = 年度（3 碼，例如 "115"）
    ri2 = 執照類別代碼（本專案固定查「使用執照」= "3"）
    ri3 = 流水號（補零至 5 碼）
    ri4 = 變更次數（補零至 2 碼，預設 "00"；若查無資料且該執照確實存在，
          可能是變更次數不是 00，但目前批次輸入沒有這個欄位，先固定 00）
    ri5 = ri1+ri2+ri3+ri4 再加上固定尾碼 "I30"
          （觀察多筆不同行政區、不同執照類別的資料，這個尾碼都是 "I30"，
          判斷是新北市建管系統整體的固定代碼，不是分區代碼）

備註：新北市另外也有一個 `building-apply.publicwork.ntpc.gov.tw/opendata/`
開頭的「全國建管系統 opendata」標準端點（其他縣市見 adapters/opendata/），
但實測不管怎麼查都回傳 `{"data":null}`，看起來這份資料沒有真的在維護／是空的，
所以新北市還是走這裡的 bm_detail.jsp 直接 URL 存取。
"""

from __future__ import annotations

import requests
import urllib3
from bs4 import BeautifulSoup

from usage_license_scraper.adapters.base import CityAdapter, LicenseNotFound, QueryType, register
from usage_license_scraper.adapters.new_taipei.parser import build_record
from usage_license_scraper.models import InputRow, LicenseRecord

urllib3.disable_warnings()

BASE_URL = "https://building-management.publicwork.ntpc.gov.tw"
LICENSE_KIND = "3"   # 使用執照
OFFICE_SUFFIX = "I30"


def build_detail_url(year: str, number: str, change_no: str = "00") -> str:
    ri1 = year.zfill(3)
    ri2 = LICENSE_KIND
    ri3 = str(int(number)).zfill(5)
    ri4 = change_no.zfill(2)
    ri5 = f"{ri1}{ri2}{ri3}{ri4}{OFFICE_SUFFIX}"
    return f"{BASE_URL}/bm_detail.jsp?ri1={ri1}&ri2={ri2}&ri3={ri3}&ri4={ri4}&ri5={ri5}"


@register("新北市")
class NewTaipeiAdapter(CityAdapter):
    city_name = "新北市"
    source = "ntpc_bm_detail"
    supported_queries = frozenset({QueryType.LICENSE_NUMBER})
    requires_captcha = False  # 明細頁不需要驗證碼，見本檔案開頭說明

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.verify = False

    def fetch(self, row: InputRow) -> LicenseRecord:
        url = build_detail_url(row.license_year, row.license_number)
        resp = self.session.get(url, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
        self.save_html(f"{row.license_year}_{row.license_number}", html)

        if "使照字號" not in html:
            raise LicenseNotFound(f"查無資料：{url}")

        return build_record(row, BeautifulSoup(html, "html.parser"))
