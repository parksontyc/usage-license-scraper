"""台南市 NBUPIC licInfo.jsp 明細頁的 HTML 解析。

輸出欄位與格式對齊 opendata 縣市（見 adapters/opendata/parser.py）與新北市：

    台南市原始值                                  ->  對齊後
    面積／比率／高度  112.39 ㎡、57.59 ％、13.5 ｍ  ->  112.39㎡、57.59％、13.5ｍ（去掉單位前的空格）
    層棟戶數  地上4層地下1層 1幢 2棟 30戶           ->  1幢2棟30戶，地上4層 地下1層
    工程造價  新台幣壹佰肆拾萬零玖仟元整（$1,409,000） ->  1409000
    隱藏欄位  ***                                  ->  空字串（系統不公開，例如多數案件的工程造價、雜項工程）
    樓層      地上層003、3.2000、64.7200            ->  地上003層、3.2ｍ、64.72㎡
    停車面積  15.0000                               ->  15㎡

地號表只有「序號／行政區／地段／地號」，沒有面積欄。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from usage_license_scraper.adapters.value_format import clean_value as _clean
from usage_license_scraper.adapters.value_format import normalize_cost, normalize_household, normalize_roc_date
from usage_license_scraper.adapters.value_format import number_with_unit as _number
from usage_license_scraper.models import (
    AddressRecord,
    FloorRecord,
    InputRow,
    LandRecord,
    LicenseRecord,
    ParkingRecord,
)


def _cell(soup: BeautifulSoup, label: str) -> str:
    """在 `<td class="name text-right">` 中找到 label，回傳緊鄰的下一個 td 文字。"""
    label = label.strip()
    for td in soup.find_all("td", class_=re.compile(r"name.*text-right")):
        if td.get_text(strip=True) == label:
            sib = td.find_next_sibling("td")
            if sib:
                return sib.get_text(" ", strip=True)
    return ""


def _val(soup: BeautifulSoup, label: str) -> str:
    return _clean(_cell(soup, label))


def _parse_address_table(soup: BeautifulSoup, license_key: str) -> list[AddressRecord]:
    records = []
    for h2 in soup.find_all(["h2", "div"], class_=re.compile(r"ltitle")):
        if "地址行政區" not in h2.get_text():
            continue
        table = h2.find_next("table")
        if not table:
            continue
        for tr in table.find_all("tr"):
            tds = [td for td in tr.find_all("td") if not td.find("input")]
            if len(tds) < 2:
                continue
            seq, addr = tds[0].get_text(strip=True), tds[1].get_text(strip=True)
            if not addr or addr == "完整地址":
                continue
            records.append(AddressRecord(license_key=license_key, seq=seq, full_address=addr))
    return records


def _parse_land_table(soup: BeautifulSoup, license_key: str) -> list[LandRecord]:
    records = []
    for h2 in soup.find_all(["h2", "div"], class_=re.compile(r"ltitle")):
        if "地段地號" not in h2.get_text():
            continue
        table = h2.find_next("table")
        if not table:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            def val(name: str) -> str:
                for td in tds:
                    if td.get("data-name") == name:
                        return td.get_text(strip=True)
                return ""

            seq = val("序號")
            if not seq:
                continue
            records.append(
                LandRecord(
                    license_key=license_key,
                    seq=seq,
                    section=val("地段"),
                    land_number=val("地號"),
                    area=_number(val("面積"), "㎡"),  # 目前看到的頁面沒有面積欄，有的話才填
                    district=val("行政區"),
                )
            )
    return records


def _parse_floor_table(soup: BeautifulSoup) -> list[FloorRecord]:
    records = []
    for h2 in soup.find_all("h2"):
        if "樓層概要" not in h2.get_text():
            continue
        table = h2.find_next("table")
        if not table:
            continue
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 8:
                continue
            floor = re.sub(r"^(地上|地下)層(\d+)$", r"\1\2層", tds[2].get_text(strip=True))  # 地上層003 -> 地上003層
            records.append(
                FloorRecord(
                    seq=tds[0].get_text(strip=True),
                    building_no=tds[1].get_text(strip=True),
                    floor=floor,
                    floor_height=_number(tds[3].get_text(strip=True), "ｍ"),
                    applied_area=_number(tds[4].get_text(strip=True), "㎡"),
                    balcony_area=_number(tds[5].get_text(strip=True), "㎡"),
                    terrace_area=_number(tds[6].get_text(strip=True), "㎡"),
                    use_group=tds[7].get_text(strip=True),
                )
            )
    return records


def _parse_parking_table(soup: BeautifulSoup) -> list[ParkingRecord]:
    records = []
    for h2 in soup.find_all("h2"):
        if "停車空間" not in h2.get_text():
            continue
        table = h2.find_next("table")
        if not table:
            continue
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            records.append(
                ParkingRecord(
                    seq=tds[0].get_text(strip=True),
                    category=tds[1].get_text(strip=True),
                    legal_or_self=tds[2].get_text(strip=True),
                    indoor_outdoor=tds[3].get_text(strip=True),
                    count=tds[4].get_text(strip=True),
                    area=_number(tds[5].get_text(strip=True), "㎡"),
                )
            )
    return records


def build_record(row: InputRow, soup: BeautifulSoup) -> LicenseRecord:
    license_key = _cell(soup, "執照字號")

    return LicenseRecord(
        community_id=row.community_id,
        city=row.city,
        district=row.district,
        license_year=row.license_year,
        license_number=row.license_number,
        license_key=license_key,
        original_license=_val(soup, "原領執照字號"),
        issue_date=normalize_roc_date(_val(soup, "發照日期")),  # 台南明細頁沒有開工／竣工日期
        builder=_val(soup, "起造人"),
        designer_name=_val(soup, "設計人(姓名)"),
        designer_firm=_val(soup, "設計人(事務所)"),
        supervisor_name=_val(soup, "監造人(姓名)"),
        supervisor_firm=_val(soup, "監造人(事務所)"),
        contractor_name=_val(soup, "承造人(姓名)"),
        contractor_firm=_val(soup, "承造人"),
        representative_address=_val(soup, "地　　址"),
        land_number=_val(soup, "地　　號"),
        use_zone=_val(soup, "使用分區"),
        site_area_arcade=_val(soup, "基地面積(騎樓)"),
        site_area_other=_val(soup, "基地面積(其他)"),
        site_area_setback=_val(soup, "基地面積(退縮地)"),
        site_area_total=_val(soup, "基地面積(合計)"),
        floor_building_household=normalize_household(_val(soup, "層棟戶數")),
        legal_open_space_area=_val(soup, "法定空地面積"),
        design_coverage_ratio=_val(soup, "設計建蔽率"),
        total_floor_area=_val(soup, "總樓地板面績"),
        design_far=_val(soup, "設計容積率"),
        building_height=_val(soup, "建物高度"),
        construction_type=_val(soup, "建造類別"),
        structure_type=_val(soup, "構造種類"),
        building_area=_val(soup, "建築面積"),
        building_area_other=_val(soup, "建築面積(其他)"),
        shelter_area_above=_val(soup, "防空避難面積(地上)"),
        shelter_area_below=_val(soup, "防空避難面積(地下)"),
        misc_engineering=_val(soup, "雜項工程"),
        construction_cost=normalize_cost(_cell(soup, "工程造價")),
        public_use_building=_val(soup, "公眾使用建築物"),
        floors=_parse_floor_table(soup),
        parkings=_parse_parking_table(soup),
        addresses=_parse_address_table(soup, license_key),
        lands=_parse_land_table(soup, license_key),
    )
