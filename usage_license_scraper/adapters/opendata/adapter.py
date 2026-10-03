"""opendata 標準端點縣市共用的 adapter 基底。"""

from __future__ import annotations

import requests

from usage_license_scraper.adapters.base import CityAdapter, LicenseNotFound, QueryType
from usage_license_scraper.adapters.opendata import client
from usage_license_scraper.adapters.opendata.parser import build_record_without_row, extract_serial, extract_year
from usage_license_scraper.models import AddressQuery, InputRow, LandQuery, LicenseRecord


class OpenDataLicenseAdapter(CityAdapter):
    """給有實作「全國建管系統 opendata」標準端點的縣市共用的 adapter 基底。

    子類別只需要設定 `city_name` 和 `base_url`（例如 "https://build.hccg.gov.tw"）。
    """

    base_url: str
    source = "opendata"
    supported_queries = frozenset(QueryType)
    requires_captcha = False

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.verify = False

    def fetch(self, row: InputRow) -> LicenseRecord:
        """年＋號剛好只對到一張（排除雜項後）時回傳該張；批次比對請用 matcher。"""
        candidates = [c for c in self.candidates_by_number(row) if not c.is_misc]
        if len(candidates) != 1:
            raise LicenseNotFound(f"{row.license_year} 年第 {row.license_number} 號對到 {len(candidates)} 張執照")
        return candidates[0]

    # ── 批次比對用的候選 ──

    def candidates_by_number(self, row: InputRow) -> list[LicenseRecord]:
        year, number = row.license_year.strip(), row.license_number.strip()
        if not (year.isdigit() and number.isdigit()):
            return []
        items = client.search_by_number(self.base_url, year, number, session=self.session)
        # API 端的 pattern 比較寬鬆，這裡再用年與號精準核對
        items = [
            i for i in items
            if extract_year(i.get("核發執照字號", "")) == year.zfill(3)
            and extract_serial(i.get("核發執照字號", "")) == str(int(number))
        ]
        return self._records(items)

    def candidates_by_key(self, row: InputRow, key: str) -> list[LicenseRecord]:
        return self._records(client.search_by_key(self.base_url, key, session=self.session))

    def candidates_by_address(self, address: AddressQuery) -> list[LicenseRecord]:
        found: dict[str, dict] = {}
        for conditions in client.address_candidate_conditions(address):
            for item in client.search(self.base_url, conditions, self.session):
                found.setdefault(item.get("核發執照字號", ""), item)
        return self._records(list(found.values()))

    # ── 回傳多筆的查詢 ──

    def fetch_year(self, year: str, license_type: str = "使用執照") -> list[LicenseRecord]:
        return self._search(client.year_conditions(year, license_type))

    def search_by_address(self, query: AddressQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        return self._search(client.address_conditions(query, license_type))

    def search_by_land(self, query: LandQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        return self._search(client.land_conditions(query, license_type))

    def _search(self, conditions: dict[str, str]) -> list[LicenseRecord]:
        return self._records(client.exclude_misc(client.search(self.base_url, conditions, self.session)))

    def _records(self, items: list[dict]) -> list[LicenseRecord]:
        return [build_record_without_row(self.city_name, item) for item in items]
