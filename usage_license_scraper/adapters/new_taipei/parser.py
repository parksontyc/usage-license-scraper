"""新北市 bm_detail.jsp 明細頁的 HTML 解析。

明細頁所有分頁（起造人／設計人／監造人／承造人／地號／樓層…）都在同一次
GET 回應的 HTML 裡（CSS 分頁籤只是前端顯示用），各分頁是 `<div id="infoN">`。

輸出欄位與格式對齊 opendata 縣市（桃園市／新竹市／台中市／高雄市，見
adapters/opendata/parser.py），讓不同縣市的結果可以直接合併比較：

    新北市原始值                         →  對齊後
    發照日期   民國115年01月05日         →  115年01月05日
    層棟戶數   1幢1棟地上5層 地下0層 共1戶 →  1幢1棟1戶，地上5層 地下0層
    建物高度   16.6Ｍ                    →  16.6ｍ
    工程造價   4717000元                 →  4717000
    防空避難   地上0㎡，地下0㎡           →  地上／地下拆成兩欄
    營造廠名稱 XX營造 負責人:OOO          →  承造人＝XX營造（完整字串留在 contractor_firm）
    停車輛數   法定/鼓勵/自行增設 三欄     →  法定3輛，獎勵0輛，自設0輛
    地號       新北市林口區新林段 216 - 2 地號 →  地段＝新北市林口區新林段、地號＝0216-0002
    樓層       3.6 / 120.06 / 棟別 --     →  3.6ｍ / 120.06㎡ / 棟別空白

新北市頁面沒有「建築面積」欄位，building_area 會是空字串。
另外有一些新北市才有的欄位（設計人／監造人事務所、騎樓地／其他／退縮地面積、
設計建蔽率／容積率、樓層的陽台／露台面積、土地面積），照樣保留。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from usage_license_scraper.district import district_from_address
from usage_license_scraper.models import (
    AddressRecord,
    FloorRecord,
    InputRow,
    LandRecord,
    LicenseRecord,
    ParkingRecord,
)


def tab(soup: BeautifulSoup, tab_id: str):
    return soup.find("div", id=tab_id)


def cell(scope, label: str) -> str:
    """在 scope 範圍內找 `<th>label</th>`，回傳緊鄰的下一個 `<td>` 文字。"""
    if scope is None:
        return ""
    for th in scope.find_all("th"):
        if th.get_text(strip=True) == label:
            td = th.find_next_sibling("td")
            if td:
                return td.get_text(" ", strip=True)
    return ""


# ── 格式對齊 opendata ──


def normalize_date(value: str) -> str:
    return value.removeprefix("民國")


def normalize_height(value: str) -> str:
    return value.replace("Ｍ", "ｍ")


def normalize_cost(value: str) -> str:
    return value.removesuffix("元")


def normalize_household(value: str) -> str:
    """'3幢7棟地上13層 地下1層 共541戶' -> '3幢7棟541戶，地上13層 地下1層'"""
    m = re.match(r"(.*?棟)\s*(地上\d+層)\s*(地下\d+層)\s*共(\d+)戶", value)
    if not m:
        return value
    buildings, above, below, households = m.groups()
    return f"{buildings}{households}戶，{above} {below}"


def split_shelter(value: str) -> tuple[str, str]:
    """'地上0㎡，地下0㎡' -> ('0㎡', '0㎡')"""
    above = re.search(r"地上\s*([\d.]+\s*㎡?)", value)
    below = re.search(r"地下\s*([\d.]+\s*㎡?)", value)
    return (above.group(1) if above else "", below.group(1) if below else "")


def company_only(value: str) -> str:
    """'岱立營造工程股份有限公司 負責人:李陳欣燕' -> '岱立營造工程股份有限公司'"""
    return re.split(r"\s*負責人\s*[:：]", value, maxsplit=1)[0].strip()


def with_unit(value: str, unit: str) -> str:
    v = value.strip()
    return f"{v}{unit}" if v and v != "--" and not v.endswith(unit) else ("" if v == "--" else v)


def split_land(text: str) -> tuple[str, str]:
    """'新北市林口區新林段 216 - 2 地號' -> ('新北市林口區新林段', '0216-0002')，
    跟 opendata 的「地段」「地號母號-地號子號（各補零 4 碼）」一致。"""
    m = re.match(r"(.+?段(?:.+?小段)?)\s*(\d+)(?:\s*-\s*(\d+))?", text)
    if not m:
        return "", text
    section, mother, child = m.groups()
    return section, f"{mother.zfill(4)}-{(child or '0').zfill(4)}"


# ── 各分頁 ──


def parse_addresses(soup: BeautifulSoup) -> list[AddressRecord]:
    """起造人分頁（info2）每一戶各有一組「起造人／幢棟層戶／使用類組／門牌號碼」，
    逐一抓出所有門牌號碼（大型社區可能有數百戶）。"""
    scope = tab(soup, "info2")
    records: list[AddressRecord] = []
    if scope is None:
        return records
    for th in scope.find_all("th"):
        if th.get_text(strip=True) != "門牌號碼":
            continue
        td = th.find_next_sibling("td")
        addr = td.get_text(" ", strip=True) if td else ""
        if addr:
            records.append(AddressRecord(seq=str(len(records) + 1), full_address=addr))
    return records


def parse_land(soup: BeautifulSoup) -> list[LandRecord]:
    scope = tab(soup, "info7")
    records: list[LandRecord] = []
    if scope is None:
        return records
    table = scope.find("table")
    if table is None:
        return records
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue  # 表頭列（全部是 th）
        section, land_number = split_land(tds[0].get_text(" ", strip=True))
        records.append(
            LandRecord(
                seq=str(len(records) + 1),
                section=section,
                land_number=land_number,
                area=tds[2].get_text(strip=True) if len(tds) > 2 else "",
            )
        )
    return records


def parse_floors(soup: BeautifulSoup) -> list[FloorRecord]:
    scope = tab(soup, "info8")
    records: list[FloorRecord] = []
    if scope is None:
        return records
    table = scope.find("table")
    if table is None:
        return records
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue  # 表頭列
        building_no = tds[0].get_text(strip=True)
        records.append(
            FloorRecord(
                seq=str(len(records) + 1),
                building_no="" if building_no == "--" else building_no,
                floor=tds[1].get_text(strip=True),
                floor_height=with_unit(tds[2].get_text(strip=True), "ｍ"),
                applied_area=with_unit(tds[3].get_text(strip=True), "㎡"),
                balcony_area=with_unit(tds[4].get_text(strip=True), "㎡"),
                terrace_area=with_unit(tds[5].get_text(strip=True), "㎡"),
                use_group=tds[6].get_text(strip=True),
            )
        )
    return records


def parse_parkings(info1) -> list[ParkingRecord]:
    legal = cell(info1, "法定停車輛數")
    bonus = cell(info1, "鼓勵停車輛數")
    self_added = cell(info1, "自行增設停車輛數")
    if not (legal or bonus or self_added):
        return []
    count = f"法定{legal or 0}輛，獎勵{bonus or 0}輛，自設{self_added or 0}輛"
    return [ParkingRecord(seq="1", count=count)]


def build_record(row: InputRow, soup: BeautifulSoup) -> LicenseRecord:
    info1 = tab(soup, "info1")   # 基地概要／建築概要／雜項／其他
    info2 = tab(soup, "info2")   # 起造人
    info4 = tab(soup, "info4")   # 設計人
    info5 = tab(soup, "info5")   # 監造人
    info6 = tab(soup, "info6")   # 承造人

    contractor = cell(info6, "營造廠名稱")
    addresses = parse_addresses(soup)
    shelter_above, shelter_below = split_shelter(cell(info1, "防空避難面積"))

    return LicenseRecord(
        community_id=row.community_id,
        city=row.city,
        district=row.district or (district_from_address(addresses[0].full_address) if addresses else ""),
        license_year=row.license_year,
        license_number=row.license_number,
        license_key=cell(soup, "使照字號"),
        original_license=cell(soup, "建照字號"),
        issue_date=normalize_date(cell(info1, "發照日期")),
        builder=cell(info2, "起造人"),
        designer_name=cell(info4, "設計人"),
        designer_firm=cell(info4, "事務所名稱"),
        supervisor_name=cell(info5, "監造人"),
        supervisor_firm=cell(info5, "事務所名稱"),
        contractor_name=company_only(contractor),
        contractor_firm=contractor,
        representative_address=cell(info2, "門牌號碼"),
        use_zone=cell(info1, "使用分區"),
        site_area_arcade=cell(info1, "騎樓地面積"),
        site_area_other=cell(info1, "其他面積"),
        site_area_setback=cell(info1, "退縮地面積"),
        site_area_total=cell(info1, "基地面積合計"),
        floor_building_household=normalize_household(cell(info1, "層棟戶數")),
        legal_open_space_area=cell(info1, "法定空地面積"),
        design_coverage_ratio=cell(info1, "設計建蔽率"),
        total_floor_area=cell(info1, "總樓地板面積"),
        design_far=cell(info1, "設計容積率"),
        building_height=normalize_height(cell(info1, "建物高度")),
        construction_type=cell(info1, "建造類別"),
        structure_type=cell(info1, "構造種類"),
        shelter_area_above=shelter_above,
        shelter_area_below=shelter_below,
        misc_engineering=cell(info1, "雜項工作物"),
        construction_cost=normalize_cost(cell(info1, "工程造價")),
        lands=parse_land(soup),
        floors=parse_floors(soup),
        addresses=addresses,
        parkings=parse_parkings(info1),
    )
