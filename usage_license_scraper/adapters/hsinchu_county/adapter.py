"""新竹縣使用執照查詢：opendata（build.hsinchu.gov.tw）＋ bupic 明細頁。

1. opendata 標準端點（見 adapters/opendata/）：不需要登入，但只更新到民國 96 年左右。
2. opendata 查不到時，用 bupic「建築執照存根查詢系統」的明細頁補：
       https://build.hsinchu.gov.tw/bupic/pages/queryInfoAction.do?INDEX_KEY=10430087700
   INDEX_KEY ＝ 年(3) ＋ 種類(1，使用執照＝3) ＋ 號(5) ＋ 變更次數(2，固定 00)。
   明細頁要用「已經在瀏覽器正常查詢過」的工作階段才看得到內容（不帶工作階段只有空的頁面框架），
   所以第一次需要 bupic 時，會請使用者先在瀏覽器查詢一筆，再貼上該工作階段的 JSESSIONID；
   直接按 Enter 則這次不使用 bupic。

   本程式只讀明細頁，不使用 bupic 的查詢功能；查詢（含驗證碼）一律由使用者在瀏覽器完成。

工作階段失效的判斷：明細頁沒有內容時，再開一次已知存在的執照（KNOWN_INDEX_KEY）確認；
也沒有內容就是工作階段失效，之後需要 bupic 的列都標成失敗、下次執行再試。
"""

from __future__ import annotations

import time

import requests
from bs4 import BeautifulSoup

from usage_license_scraper.adapters.base import register
from usage_license_scraper.adapters.hsinchu_county.parser import build_record, decode, has_license
from usage_license_scraper.adapters.opendata.adapter import OpenDataLicenseAdapter
from usage_license_scraper.models import InputRow, LicenseRecord

BUPIC_BASE = "https://build.hsinchu.gov.tw/bupic"
BUPIC_QUERY_PAGE = f"{BUPIC_BASE}/preLoginFormAction.do"
LICENSE_KIND = "3"            # 使用執照
KNOWN_INDEX_KEY = "07730059400"  # (077)建都字第00594號，確認工作階段是否有效用
REQUEST_INTERVAL = 1.5        # 每次開明細頁至少間隔幾秒
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def make_index_key(year: str, number: str, change_no: str = "00") -> str:
    """('104', '877') -> '10430087700'"""
    return f"{year.zfill(3)}{LICENSE_KIND}{str(int(number)).zfill(5)}{change_no.zfill(2)}"


class BupicSessionError(RuntimeError):
    """沒有可用的 bupic 工作階段（使用者略過或已失效）；這一列標成失敗，下次執行再試。"""


@register("新竹縣")
class HsinchuCountyAdapter(OpenDataLicenseAdapter):
    city_name = "新竹縣"
    base_url = "https://build.hsinchu.gov.tw"

    def __init__(self) -> None:
        super().__init__()
        self.bupic: requests.Session | None = None
        self.bupic_state = "not_asked"  # not_asked／on／skipped／expired
        self._last_request = 0.0
        self._bupic_keys: set[str] = set()  # 從 bupic 查到的執照字號

    # ── 年號查詢：opendata 查不到才用 bupic ──

    def candidates_by_number(self, row: InputRow) -> list[LicenseRecord]:
        candidates = super().candidates_by_number(row)
        if candidates:
            return candidates
        record = self._bupic_record(row)
        return [record] if record else []

    def candidates_by_key(self, row: InputRow, key: str) -> list[LicenseRecord]:
        candidates = super().candidates_by_key(row, key)
        if candidates or not (row.license_year.isdigit() and row.license_number.isdigit()):
            return candidates
        record = self._bupic_record(row)
        return [record] if record else []

    def record_source(self, record: LicenseRecord) -> str:
        return "hsinchu_bupic" if record.license_key in self._bupic_keys else self.source

    # ── bupic ──

    def _bupic_record(self, row: InputRow) -> LicenseRecord | None:
        if not (row.license_year.isdigit() and row.license_number.isdigit()):
            return None
        self._ensure_bupic()
        index_key = make_index_key(row.license_year, row.license_number)
        soup = self._detail(index_key)
        if has_license(soup):
            record = build_record(row, soup)
            self._bupic_keys.add(record.license_key)
            return record
        # 沒內容：查無此執照，或工作階段失效。開一張已知存在的確認。
        if not has_license(self._detail(KNOWN_INDEX_KEY, save=False)):
            self.bupic_state = "expired"
            self.bupic = None
            raise BupicSessionError(
                "bupic 工作階段已失效。請在瀏覽器重新查詢一筆、複製新的 JSESSIONID，再執行一次（這些列會自動重試）"
            )
        return None

    def _ensure_bupic(self) -> None:
        if self.bupic_state == "on":
            return
        if self.bupic_state == "not_asked":
            self.bupic_state = "skipped"
            print(
                "\n[新竹縣] opendata 只更新到民國 96 年左右，查不到的改用 bupic 明細頁。\n"
                f"  1. 用瀏覽器開 {BUPIC_QUERY_PAGE} ，正常查詢任意一筆（輸入驗證碼）\n"
                "  2. F12 → Application → Cookies → build.hsinchu.gov.tw，複製 JSESSIONID 的值"
            )
            # 用一般輸入（看得到貼上的內容）：JSESSIONID 只是瀏覽器的暫時工作階段，隱藏輸入反而讓人以為貼不上
            session_id = input("  貼上 JSESSIONID 後按 Enter（直接按 Enter 則這次不使用 bupic）：").strip()
            if session_id:
                self.bupic = requests.Session()
                self.bupic.verify = False
                self.bupic.headers.update({"User-Agent": USER_AGENT, "Referer": BUPIC_QUERY_PAGE})
                self.bupic.cookies.set("JSESSIONID", session_id, domain="build.hsinchu.gov.tw", path="/bupic")
                if has_license(self._detail(KNOWN_INDEX_KEY, save=False)):
                    self.bupic_state = "on"
                    print("  bupic 工作階段可以使用")
                    return
                self.bupic = None
                print("  這個 JSESSIONID 打不開明細頁（可能還沒在瀏覽器查詢過，或已經失效），這次不使用 bupic")
        reason = "這次執行略過了 bupic" if self.bupic_state == "skipped" else "bupic 工作階段已失效"
        raise BupicSessionError(f"opendata 查無資料，{reason}；下次提供 bupic 工作階段時會再查")

    def _detail(self, index_key: str, save: bool = True) -> BeautifulSoup:
        wait = REQUEST_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        resp = self.bupic.get(f"{BUPIC_BASE}/pages/queryInfoAction.do", params={"INDEX_KEY": index_key}, timeout=30)
        self._last_request = time.monotonic()
        resp.raise_for_status()
        html = decode(resp.content)
        if save:
            self.save_html(index_key, html)
        return BeautifulSoup(html, "html.parser")
