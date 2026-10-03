"""把 opendata API 回傳的 JSON 項目轉成 LicenseRecord。

回傳的 JSON 欄位名稱在桃園市／新竹市／台中市／高雄市之間幾乎完全相同
（同一套規格）。已知差異：高雄市沒有「建築物用途」「土地使用分區」
「起造人地址」欄位，對應的 LicenseRecord 欄位會是空字串。
"""

from __future__ import annotations

import re

from usage_license_scraper.district import district_from_address
from usage_license_scraper.models import (
    AddressRecord,
    FloorRecord,
    InputRow,
    LandRecord,
    LicenseRecord,
    ParkingRecord,
)


def extract_serial(license_key: str) -> str:
    """從「核發執照字號」尾端抓出流水號數字，例如
    "(115)桃市都施使字第德00001號" -> "1"。抓不到就回傳空字串。"""
    m = re.search(r"(\d+)號$", license_key)
    return str(int(m.group(1))) if m else ""


def extract_year(license_key: str) -> str:
    """從「核發執照字號」開頭抓出年度，"(115)桃市..." 或 "115中都..." -> "115"。"""
    m = re.match(r"\(?(\d{2,3})\)?", license_key)
    return m.group(1).zfill(3) if m else ""


def parse_floors(items: list[dict]) -> list[FloorRecord]:
    records = []
    for i, item in enumerate(items, start=1):
        records.append(
            FloorRecord(
                seq=str(i),
                floor=item.get("樓層別", ""),
                floor_height=item.get("樓層高度", ""),
                applied_area=item.get("樓層面積", ""),
                use_group=item.get("樓層用途", ""),
            )
        )
    return records


def parse_lands(items: list[dict]) -> list[LandRecord]:
    records = []
    for i, item in enumerate(items, start=1):
        mother = item.get("地號母號", "")
        child = item.get("地號子號", "")
        land_number = f"{mother}-{child}" if mother or child else ""
        records.append(
            LandRecord(
                seq=str(i),
                section=f"{item.get('行政區', '')}{item.get('地段', '')}",
                land_number=land_number,
            )
        )
    return records


def parse_addresses(items: list[dict]) -> list[AddressRecord]:
    records = []
    for i, item in enumerate(items, start=1):
        full = "".join(item.get(k, "") for k in ("行政區", "村里鄰", "路街段巷弄", "號", "樓"))
        if not full:
            continue
        records.append(AddressRecord(seq=str(i), full_address=full))
    return records


def build_record(row: InputRow, item: dict) -> LicenseRecord:
    addresses = item.get("門牌") or []
    household_desc = (
        f"{item.get('棟數', '')}{item.get('戶數', '')}，"
        f"地上{item.get('地上層數', '')} 地下{item.get('地下層數', '')}"
    )
    return LicenseRecord(
        community_id=row.community_id,
        city=row.city,
        district=row.district or (district_from_address(addresses[0].get("行政區", "")) if addresses else ""),
        license_year=row.license_year,
        license_number=row.license_number,
        license_key=item.get("核發執照字號", ""),
        original_license=item.get("原領執照字號", ""),
        issue_date=item.get("發照日期", ""),
        builder=item.get("起造人代表人", ""),
        designer_name=item.get("設計人", ""),
        supervisor_name=item.get("監造人", ""),
        contractor_name=item.get("承造人", ""),
        use_zone=item.get("土地使用分區", ""),
        site_area_total=item.get("基地面積", ""),
        legal_open_space_area=item.get("法定空地面積", ""),
        total_floor_area=item.get("總樓地板面積", ""),
        building_height=item.get("建築物高度", ""),
        construction_type=item.get("建造類別", ""),
        structure_type=item.get("構造別", ""),
        building_area=item.get("建築面積", ""),
        shelter_area_below=item.get("地下避難面積", ""),
        misc_engineering=item.get("雜項工作物", ""),
        construction_cost=item.get("工程造價", ""),
        floor_building_household=household_desc,
        floors=parse_floors(item.get("樓層概要") or []),
        lands=parse_lands(item.get("地號") or []),
        addresses=parse_addresses(addresses),
        parkings=[ParkingRecord(seq="1", count=item.get("停車空間", ""))] if item.get("停車空間") else [],
    )


def build_record_without_row(city: str, item: dict) -> LicenseRecord:
    """整年度／門牌／地號查詢沒有輸入列，年、號、行政區從回傳資料推回來。"""
    license_key = item.get("核發執照字號", "")
    row = InputRow(
        community_id="",
        city=city,
        district="",  # build_record 會從門牌補上
        address="",
        license_year=extract_year(license_key),
        license_number=extract_serial(license_key),
    )
    return build_record(row, item)
