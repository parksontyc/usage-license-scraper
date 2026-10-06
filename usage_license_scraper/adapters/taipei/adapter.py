"""台北市使用執照查詢：查本機索引（台北市開放資料，見 index.py），不需要連線查詢或驗證碼。

第一次查台北市時會自動建立索引；之後每個月第一次查詢時，自動下載 config/taipei.toml
列出的當年度資料並更新索引。比對流程跟其他縣市相同（字號、門牌、行政區，見 matcher.py）。
"""

from __future__ import annotations

from usage_license_scraper.adapters.base import CityAdapter, LicenseNotFound, QueryType, register
from usage_license_scraper.adapters.taipei.index import TaipeiIndex
from usage_license_scraper.models import AddressQuery, InputRow, LandQuery, LicenseRecord

SOURCE = "taipei_opendata"


@register("台北市")
class TaipeiAdapter(CityAdapter):
    city_name = "台北市"
    source = SOURCE
    supported_queries = frozenset(QueryType)
    requires_captcha = False

    def __init__(self) -> None:
        self._index: TaipeiIndex | None = None
        self._duplicated: set[str] = set()  # 來源資料裡有重複紀錄的字號

    @property
    def index(self) -> TaipeiIndex:
        if self._index is None:
            self._index = TaipeiIndex()
            self._index.ensure_ready()
        return self._index

    def close(self) -> None:
        if self._index is not None:
            self._index.close()

    def record_source(self, record: LicenseRecord) -> str:
        if record.license_key in self._duplicated:
            return f"{SOURCE}（來源資料重複，取欄位較完整的一筆）"
        return SOURCE

    def _with_row(self, row: InputRow | None, found: list[tuple[LicenseRecord, int]]) -> list[LicenseRecord]:
        records = []
        for record, duplicates in found:
            if duplicates > 1:
                self._duplicated.add(record.license_key)
            if row is not None:
                record.community_id = row.community_id
            records.append(record)
        return records

    # ── 年號／字號／門牌（批次比對用） ──

    def fetch(self, row: InputRow) -> LicenseRecord:
        candidates = self.candidates_by_number(row)
        if len(candidates) != 1:
            raise LicenseNotFound(f"{row.license_year} 年第 {row.license_number} 號對到 {len(candidates)} 張執照")
        return candidates[0]

    def candidates_by_number(self, row: InputRow) -> list[LicenseRecord]:
        if not (row.license_year.isdigit() and row.license_number.isdigit()):
            return []
        return self._with_row(row, self.index.by_number(row.license_year, row.license_number))

    def candidates_by_key(self, row: InputRow, key: str) -> list[LicenseRecord]:
        found = self.index.by_key(key)
        if not found and row.license_year.isdigit() and row.license_number.isdigit():
            found = self.index.by_number(row.license_year, row.license_number)  # 由 matcher 比對字號寫法
        return self._with_row(row, found)

    def candidates_by_address(self, address: AddressQuery) -> list[LicenseRecord]:
        return self._with_row(None, self.index.by_address(address.road, address.number))

    # ── 整年度／門牌／地號 ──

    def fetch_year(self, year: str, license_type: str = "使用執照") -> list[LicenseRecord]:
        return self._with_row(None, self.index.by_year(year))

    def search_by_address(self, query: AddressQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        from usage_license_scraper.matcher import address_matches, parse_address_specs  # 避免循環 import

        specs = parse_address_specs(f"{query.road}{query.number}號" if query.number else query.road, query.district)
        found: dict[str, LicenseRecord] = {}
        for q in dict.fromkeys(q for s in specs for q in s.api_queries()):
            for record in self.candidates_by_address(q):
                found.setdefault(record.license_key, record)
        return [r for r in found.values() if address_matches(specs, r)]

    def search_by_land(self, query: LandQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        return self._with_row(None, self.index.by_land(query.section, query.main_no, query.sub_no))
