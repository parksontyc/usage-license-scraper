"""台北市使用執照開放資料（XML）解析。有兩種格式：

舊格式（40.xml～89.xml，民國 036～089 年）：
    <使照><執照 號碼="075使字第0002號"><發照日期>0750103</發照日期><原建照>…
        <全建築地點><建築地點>…</建築地點></全建築地點>
        <全地段地號><地段地號>…</地段地號></全地段地號>
        <全停車空間><停車空間>…</停車空間></全停車空間> …
新格式（Taipei_90_114.xml、Taipei_<年度>.xml）：
    <Datas><Data><執照號碼>115使字第0001號</執照號碼><原核發執照>…<工程金額>…
        <建築地點><地址>…</地址></建築地點>
        <地段地號><地段號>…</地段號></地段地號>
        <停車空間><停車空間說明>…</停車空間說明></停車空間> …
兩種格式的樓層都在 <建築概要><樓層>，雜項在 <雜項工作物><說明>。

開放資料沒有起造人欄位（應是個資考量），builder 會是空字串。

輸出格式對齊其他縣市（見 adapters/value_format.py）：
    發照／開工／竣工日期  1150102                          ->  115年01月02日
    設計人、監造人        邱垂睿(邱垂睿建築師事務所)        ->  姓名 邱垂睿、事務所 邱垂睿建築師事務所
    承造人                楊勝飛(友業營造股份有限公司)      ->  contractor_name＝友業營造股份有限公司（同其他縣市放營造廠）
    構造種類              鋼骨RC造(供公眾使用建築物)        ->  鋼骨RC造，public_use_building＝是
    層棟戶數              幢1 棟2 地上14 地下4 戶37         ->  1幢2棟37戶，地上14層 地下4層
    面積                  783 或 783㎡                      ->  783㎡；基地面積合計＝騎樓＋其他
    建物高度              49.56M                            ->  49.56ｍ
    地段地號              臺北市中山區長安段三小段0733-0000號 -> 行政區 中山區、地段 長安段三小段、地號 0733-0000
    樓層                  地下1樓,面積:702.35㎡,高度:4.71M,用途:… -> 地下001層、702.35㎡、4.71ｍ、用途
早期資料常常整筆面積都是 0（沒有填），這種情況面積欄位一律留空。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

from usage_license_scraper.adapters.value_format import normalize_roc_date, number_with_unit
from usage_license_scraper.district import district_from_address
from usage_license_scraper.license_no import parse_license_no
from usage_license_scraper.models import (
    AddressRecord,
    FloorRecord,
    LandRecord,
    LicenseRecord,
    ParkingRecord,
)

CITY = "台北市"


def iter_records(path: str | Path) -> Iterator[LicenseRecord]:
    """逐筆讀出一個 XML 檔的所有執照（用 iterparse，68 MB 的檔案也不會一次全部讀進記憶體）。"""
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag in ("Data", "執照"):
            record = build_record(el)
            el.clear()
            if record.license_key:
                yield record


def _text(el: ET.Element, *tags: str) -> str:
    for tag in tags:
        v = el.findtext(tag)
        if v and v.strip():
            return v.strip()
    return ""


def _children(el: ET.Element, parent_tags: tuple[str, ...]) -> list[str]:
    for tag in parent_tags:
        parent = el.find(tag)
        if parent is not None:
            return [(c.text or "").strip() for c in parent if (c.text or "").strip()]
    return []


def split_person(value: str) -> tuple[str, str]:
    """'邱垂睿(邱垂睿建築師事務所)' -> ('邱垂睿', '邱垂睿建築師事務所')；'趙奕翔建築師事務所' -> ('', '趙奕翔建築師事務所')"""
    v = value.strip()
    m = re.fullmatch(r"(.*?)[(（](.+)[)）]", v)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return ("", v) if v.endswith("事務所") else (v, "")


def split_structure(value: str) -> tuple[str, str]:
    """'鋼骨RC造(供公眾使用建築物)' -> ('鋼骨RC造', '是')；'RC造、' -> ('RC造', '')"""
    v = value.strip().strip("、").strip()
    public = ""
    m = re.search(r"[(（]([^()（）]*公眾使用[^()（）]*)[)）]", v)
    if m:
        public = "否" if "非" in m.group(1) else "是"
        v = (v[: m.start()] + v[m.end():]).strip("、 ")
    return v, public


def _area(value: str) -> str:
    v = value.strip().removesuffix("㎡").strip()
    return number_with_unit(v, "㎡") if v else ""


def _area_number(value: str) -> float:
    try:
        return float(value.strip().removesuffix("㎡"))
    except ValueError:
        return 0.0


def parse_land(text: str, seq: int) -> LandRecord:
    """'臺北市中山區長安段三小段0733-0000號' -> 行政區 中山區、地段 長安段三小段、地號 0733-0000"""
    t = text.strip().removesuffix("號")
    district = district_from_address(t)
    m = re.match(r"^(?:\S{2}[市縣])?(?:\S+?[區鄉鎮市])?(.+?段(?:.+?小段)?)(\d+)-?(\d*)$", t)
    if not m:
        return LandRecord(seq=str(seq), district=district, land_number=text.strip())
    section, main, sub = m.groups()
    return LandRecord(
        seq=str(seq), district=district, section=section, land_number=f"{main.zfill(4)}-{(sub or '0').zfill(4)}"
    )


def parse_floor(text: str, seq: int) -> FloorRecord:
    """'地下1樓,面積:702.35㎡,高度:4.71M,用途:(防空避難室兼停車空間),(第二組)多戶住宅'
    '1棟地下001樓,面積:321.43㎡,高度:2.9M,用途:防空避難室'"""
    m = re.match(r"^(?P<building>[^,]*?棟)?(?P<floor>[^,]*?),面積:(?P<area>[^,]*),高度:(?P<height>[^,]*),用途:(?P<use>.*)$", text)
    if not m:
        return FloorRecord(seq=str(seq), floor=text)
    floor = re.sub(r"^(地上|地下)0*(\d+)樓$", lambda f: f"{f.group(1)}{int(f.group(2)):03d}層", m.group("floor"))
    height = m.group("height").strip().rstrip("Mm")
    return FloorRecord(
        seq=str(seq),
        building_no=(m.group("building") or "").removesuffix("棟"),
        floor=floor,
        floor_height=number_with_unit(height, "ｍ") if height else "",
        applied_area=_area(m.group("area")),
        use_group=m.group("use").strip(),
    )


def parse_parking(text: str, seq: int) -> ParkingRecord:
    """'設置類別:平面,車位分類:機車,檢討類別:法定,室內,地下,輛數:19,面積:37.62㎡'"""
    fields: dict[str, str] = {}
    loose: list[str] = []
    for part in text.split(","):
        key, sep, value = part.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
        elif part.strip():
            loose.append(part.strip())
    return ParkingRecord(
        seq=str(seq),
        category="／".join(v for v in (fields.get("設置類別", ""), fields.get("車位分類", "")) if v),
        legal_or_self=fields.get("檢討類別", ""),
        indoor_outdoor="／".join(loose),
        count=fields.get("輛數", ""),
        area=_area(fields.get("面積", "")),
    )


def build_record(el: ET.Element) -> LicenseRecord:
    license_key = (el.get("號碼") or _text(el, "執照號碼")).strip()
    parsed = parse_license_no(license_key)

    designer_name, designer_firm = split_person(_text(el, "設計人"))
    supervisor_name, supervisor_firm = split_person(_text(el, "監造人"))
    contractor = _text(el, "承造人")
    person, company = split_person(contractor)
    structure, public_use = split_structure(_text(el, "構造種類"))

    info = el.find("建物資訊")
    def count(tag: str) -> str:
        v = (info.findtext(tag) or "").strip() if info is not None else ""
        return v if v and v != "0" else ""
    zhuang, dong, hu, above, below = (count(t) for t in ("幢數", "棟數", "戶數", "地上層數", "地下層數"))
    household = ""
    if dong or hu or above:  # 早期資料常常全部是 0（沒有填）
        head = (f"{zhuang}幢" if zhuang else "") + (f"{dong}棟" if dong else "") + (f"{hu}戶" if hu else "")
        household = f"{head}，地上{above or 0}層 地下{below or 0}層"

    areas = el.find("建物面積")
    area = {c.tag: (c.text or "").strip() for c in areas} if areas is not None else {}
    all_zero = not any(_area_number(v) for v in area.values())
    def a(tag: str) -> str:
        return "" if all_zero else _area(area.get(tag, ""))
    total = _area_number(area.get("騎樓基地面積", "")) + _area_number(area.get("其他基地面積", ""))

    height = _text(el, "建物高度").rstrip("Mm")
    cost = _text(el, "工程金額", "工程造價")
    addresses = _children(el, ("建築地點", "全建築地點"))

    return LicenseRecord(
        city=CITY,
        district=district_from_address(addresses[0]) if addresses else "",
        license_year=parsed.year if parsed else "",
        license_number=parsed.number if parsed else "",
        license_key=license_key,
        original_license=_text(el, "原核發執照", "原建照"),
        issue_date=normalize_roc_date(_text(el, "發照日期")),
        start_date=normalize_roc_date(_text(el, "開工日期")),
        completion_date=normalize_roc_date(_text(el, "竣工日期")),
        designer_name=designer_name,
        designer_firm=designer_firm,
        supervisor_name=supervisor_name,
        supervisor_firm=supervisor_firm,
        contractor_name=company or person,
        contractor_firm=contractor,
        representative_address=addresses[0] if addresses else "",
        use_zone=_text(el, "使用分區"),
        site_area_arcade=a("騎樓基地面積"),
        site_area_other=a("其他基地面積"),
        site_area_total="" if all_zero or not total else number_with_unit(f"{total:.2f}", "㎡"),
        floor_building_household=household,
        legal_open_space_area=a("法定空地面積"),
        building_area=a("建築面積"),
        shelter_area_above=a("地上避難面積"),
        shelter_area_below=a("地下避難面積"),
        building_height=number_with_unit(height, "ｍ") if height else "",
        construction_type=_text(el, "建造類別"),
        structure_type=structure,
        public_use_building=public_use,
        misc_engineering="；".join(_children(el, ("雜項工作物",))),
        construction_cost="" if not cost or cost == "0" else cost,
        addresses=[
            AddressRecord(seq=str(i), full_address=addr, district=district_from_address(addr))
            for i, addr in enumerate(addresses, start=1)
        ],
        lands=[parse_land(text, i) for i, text in enumerate(_children(el, ("地段地號", "全地段地號")), start=1)],
        floors=[parse_floor(text, i) for i, text in enumerate(_children(el, ("建築概要",)), start=1)],
        parkings=[parse_parking(text, i) for i, text in enumerate(_children(el, ("停車空間", "全停車空間")), start=1)],
    )
