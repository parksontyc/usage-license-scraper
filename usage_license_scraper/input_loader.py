"""讀取使用者批次上傳的查詢條件（CSV 或 Excel）。表頭不分大小寫，支援兩種格式：

主要格式（例如 data/input/community_from_images.csv）：
    COMMUNITY_NO, CASE_NAME, CITY, DIST, ADDR_NO, LICENSE_NO, USE_FOR
    - ADDR_NO 不含縣市、行政區，可以是範圍或多個門牌（302~312號、A、B號、(全)）
    - LICENSE_NO 是使用執照字號，年、字、號由此解析（見 license_no.py），寫法不必統一

舊格式：
    社區編號, 縣市, 行政區, 代表號地址, 使用執照年, 使用執照號

兩種格式都可以加選填的「完整字號」（或 FULL_LICENSE_KEY）欄：同一個年號對到多張執照、
列入待確認時，人工確認後把系統上的字號填在這裡，下次執行就只認這個字號。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from usage_license_scraper.license_no import parse_license_no
from usage_license_scraper.models import InputRow
from usage_license_scraper.normalize import normalize_address, normalize_city, normalize_text

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "community_id": ["COMMUNITY_NO", "社區編號", "community_id"],
    "city": ["CITY", "縣市"],
    "district": ["DIST", "行政區", "district"],
    "address": ["ADDR_NO", "代表號地址", "address"],
}

OPTIONAL_COLUMNS: dict[str, list[str]] = {
    "license_text": ["LICENSE_NO", "使用執照字號"],
    "license_year": ["使用執照年", "license_year"],
    "license_number": ["使用執照號", "使用照執號", "license_number"],
    "full_license_key": ["完整字號", "FULL_LICENSE_KEY"],
    "case_name": ["CASE_NAME", "案名", "社區名稱"],
    "use_for": ["USE_FOR"],
}


def _find(columns: list[str], aliases: list[str]) -> str | None:
    wanted = {a.lower() for a in aliases}
    return next((c for c in columns if c.strip().lower() in wanted), None)


def _resolve_columns(columns: list[str]) -> dict[str, str]:
    resolved = {}
    for field, aliases in REQUIRED_COLUMNS.items():
        match = _find(columns, aliases)
        if match is None:
            raise ValueError(f"找不到欄位「{field}」，可接受的表頭名稱為：{aliases}\n目前檔案表頭為：{columns}")
        resolved[field] = match
    for field, aliases in OPTIONAL_COLUMNS.items():
        match = _find(columns, aliases)
        if match is not None:
            resolved[field] = match
    if "license_text" not in resolved and not {"license_year", "license_number"} <= resolved.keys():
        raise ValueError(f"需要 LICENSE_NO 欄，或是「使用執照年」＋「使用執照號」兩欄\n目前檔案表頭為：{columns}")
    return resolved


def load_input_rows(path: str | Path) -> list[InputRow]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")

    df = df.fillna("")
    col_map = _resolve_columns(list(df.columns))

    rows: list[InputRow] = []
    for _, r in df.iterrows():
        def get(field: str) -> str:
            return str(r[col_map[field]]).strip() if field in col_map else ""

        year, number = get("license_year"), get("license_number")
        license_text = get("license_text")
        if license_text and not (year and number):
            parsed = parse_license_no(license_text)
            if parsed:
                year, number = parsed.year, parsed.number
        rows.append(
            InputRow(
                community_id=get("community_id"),
                city=normalize_city(get("city")),
                input_city=get("city"),
                district=normalize_text(get("district")),
                address=normalize_address(get("address")),
                input_address=get("address"),
                license_year=year,
                license_number=number,
                full_license_key=get("full_license_key"),
                license_text=license_text,
                case_name=get("case_name"),
                use_for=get("use_for"),
            )
        )
    return rows
