"""新竹縣 bupic 明細頁（queryInfoAction.do?INDEX_KEY=…）的 HTML 解析。頁面編碼是 Big5。

基本資料區是 `div.tableCon`：每一大項是一個 div，標題在 `div.tit_01`（例如「基地概要」），
底下是「小標題 span（tit_02／tit_03／tit_05）＋ 值 span.conlist」的組合；有些小項外面再包一層
（例如「基地面積」底下的騎樓地／其他／退縮地／合計），用 (大項, 外層小標題, 小標題) 取值。

明細表（門牌、地號、樓層、停車）是 `table`，依表頭辨識（頁面同時有分段與合併兩種版本，
取合併版）。

輸出格式對齊 opendata 縣市（見 adapters/value_format.py）：
    面積／比率／高度  1646.79 ㎡、49.72 ％、13.3 ｍ  ->  1646.79㎡、49.72％、13.3ｍ
    層棟戶數  地上4層 地下1層 4幢 17棟 17戶          ->  4幢17棟17戶，地上4層 地下1層
    工程造價  新台幣貳仟零柒拾萬參仟元整($20,703,000) ->  20703000
    隱藏欄位  * * *、***                              ->  空字串
    承造人    營造廠「吉米營造有限公司（登記證號：A10798）」 -> contractor_name＝吉米營造有限公司
    地號      529-1                                   ->  0529-0001
    公眾使用  非公眾使用建築物                        ->  否
施工進度、保留地目前資料庫沒有欄位，略過。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from usage_license_scraper.adapters.value_format import (
    clean_value,
    normalize_cost,
    normalize_household,
    normalize_roc_date,
    number_with_unit,
)
from usage_license_scraper.district import district_from_address
from usage_license_scraper.models import (
    AddressRecord,
    FloorRecord,
    InputRow,
    LandRecord,
    LicenseRecord,
    ParkingRecord,
)

_LABEL_CLASSES = {"tit_02", "tit_03", "tit_05"}


def decode(content: bytes) -> str:
    """頁面宣告 Big5；cp950 是 Big5 的超集，避免少數字元解不出來。"""
    return content.decode("cp950", errors="replace")


def basic_fields(soup: BeautifulSoup) -> dict[tuple[str, str, str], str]:
    """{(大項, 外層小標題, 小標題): 值}。例如 ('基地概要', '基地面積', '合計') -> '1646.79 ㎡'。"""
    fields: dict[tuple[str, str, str], str] = {}
    container = soup.find("div", class_="tableCon")
    if container is None:
        return fields
    for section_div in container.find_all("div", recursive=False):
        title = section_div.find("div", class_="tit_01")
        section = title.get_text(strip=True).rstrip("：:") if title else ""
        for value in section_div.find_all("span", class_="conlist"):
            label_span = value.find_previous_sibling("span")
            label = label_span.get_text(strip=True) if label_span and _is_label(label_span) else ""
            group = value.find_parent("span", class_="pp")
            outer_span = group.find_previous_sibling("span") if group else None
            outer = outer_span.get_text(strip=True) if outer_span and _is_label(outer_span) else ""
            fields.setdefault((section, outer, label), value.get_text(" ", strip=True))
    return fields


def _is_label(span) -> bool:
    return bool(_LABEL_CLASSES & set(span.get("class") or []))


def _tables(soup: BeautifulSoup) -> dict[tuple[str, ...], list[list[str]]]:
    """{表頭: 資料列}，表頭相同的表只取第一個。"""
    tables: dict[tuple[str, ...], list[list[str]]] = {}
    for table in soup.find_all("table"):
        header = tuple(th.get_text(strip=True) for th in table.find_all("th"))
        rows = [[td.get_text(" ", strip=True) for td in tr.find_all("td")] for tr in table.find_all("tr")]
        tables.setdefault(header, [r for r in rows if r])
    return tables


def split_land_no(value: str) -> str:
    """'529-1' -> '0529-0001'；'529' -> '0529-0000'（同 opendata 的母號-子號各補 4 碼）"""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", value.strip())
    if not m:
        return value
    return f"{m.group(1).zfill(4)}-{(m.group(2) or '0').zfill(4)}"


def company_only(value: str) -> str:
    """'吉米營造有限公司（登記證號：A10798）' -> '吉米營造有限公司'"""
    return re.split(r"\s*[（(]\s*登記證號", value, maxsplit=1)[0].strip()


def has_license(soup: BeautifulSoup) -> bool:
    """明細頁有沒有執照內容（工作階段無效或查無此 INDEX_KEY 時只會有空的頁面框架）。"""
    return bool(basic_fields(soup).get(("使用執照號碼", "", "")))


def build_record(row: InputRow, soup: BeautifulSoup) -> LicenseRecord:
    f = basic_fields(soup)

    def get(section: str, label: str = "", outer: str = "") -> str:
        return clean_value(f.get((section, outer, label), ""))

    tables = _tables(soup)
    addresses = [
        AddressRecord(seq=r[0], full_address=r[1], district=district_from_address(r[1]))
        for r in tables.get(("序號", "建築門牌"), [])
        if len(r) >= 2 and r[1]
    ]
    lands = [
        LandRecord(seq=r[0], district=district_from_address(r[1]), section=r[2], land_number=split_land_no(r[3]))
        for r in tables.get(("序號", "行政區", "地段", "地號"), [])
        if len(r) >= 4
    ]
    floors = [
        FloorRecord(
            seq=r[0],
            building_no=r[1],
            floor=r[2],
            floor_height=number_with_unit(r[3], "ｍ"),
            applied_area=number_with_unit(r[4], "㎡"),
            balcony_area=number_with_unit(r[5], "㎡"),
            terrace_area=number_with_unit(r[6], "㎡"),
            use_group=r[7],
        )
        for r in tables.get(("序號", "棟別", "層別", "樓層高度", "申請面積", "陽台面積", "露台面積", "使用類組"), [])
        if len(r) >= 8
    ]
    parkings = [
        ParkingRecord(
            seq=r[0],
            legal_or_self=r[1],
            category="／".join(v for v in (r[2], r[3]) if v),
            indoor_outdoor="／".join(v for v in (r[4], r[5]) if v),
            count=r[6],
            area=number_with_unit(r[7], "㎡"),
        )
        for r in tables.get(("序號", "法定/自設", "設置類別", "車位類別", "室內外", "地上/下", "輛數", "面積"), [])
        if len(r) >= 8
    ]

    contractor_firm = get("承造人", "營造廠")
    building_section = " ".join(k[2] + v for k, v in f.items() if k[0] == "建物概要")
    public_use = "否" if "非公眾使用建築物" in building_section else ("是" if "公眾使用建築物" in building_section else "")

    return LicenseRecord(
        community_id=row.community_id,
        city=row.city,
        district=row.district,
        license_year=row.license_year,
        license_number=row.license_number,
        license_key=get("使用執照號碼"),
        original_license=get("建造執照號碼"),
        issue_date=normalize_roc_date(get("建物概要", "發照日期")),
        start_date=normalize_roc_date(get("建築執照", "開工日期")),
        completion_date=normalize_roc_date(get("建築執照", "竣工日期")),
        builder=get("起造人", "姓名"),
        designer_name=get("設計人", "姓名"),
        designer_firm=get("設計人", "事務所"),
        supervisor_name=get("監造人", "姓名"),
        supervisor_firm=get("監造人", "事務所"),
        contractor_name=company_only(contractor_firm) or get("承造人", "姓名"),
        contractor_firm=contractor_firm,
        representative_address=get("基地概要", "地址"),
        land_number=get("基地概要", "地號"),
        use_zone=get("基地概要", "使用分區"),
        site_area_arcade=get("基地概要", "騎樓地", "基地面積"),
        site_area_other=get("基地概要", "其他", "基地面積"),
        site_area_setback=get("基地概要", "退縮地", "基地面積"),
        site_area_total=get("基地概要", "合計", "基地面積"),
        floor_building_household=normalize_household(get("建物概要", "層棟戶數")),
        legal_open_space_area=get("建物概要", "法定空地面積"),
        design_coverage_ratio=get("建物概要", "設計建蔽率"),
        total_floor_area=get("建物概要", "總樓地板面積"),
        design_far=get("建物概要", "設計容積率"),
        building_height=get("建物概要", "建物高度"),
        construction_type=get("建物概要", "建造類別"),
        structure_type=get("建物概要", "構造種類"),
        building_area=get("建物概要", "其他", "建築面積"),
        building_area_other=get("建物概要", "騎樓面積", "建築面積"),
        shelter_area_above=get("建物概要", "地上", "防空避難面積"),
        shelter_area_below=get("建物概要", "地下", "防空避難面積"),
        misc_engineering=get("建物概要", "雜項工程"),
        construction_cost=normalize_cost(f.get(("建物概要", "", "工程造價"), "")),
        public_use_building=public_use,
        floors=floors,
        parkings=parkings,
        addresses=addresses,
        lands=lands,
    )
