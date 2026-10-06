"""跑批次前先檢查輸入檔：每一列正規化、解析後的結果，以及可能的問題。

不會查詢任何網站、也不會動到資料庫，只輸出 input_check.csv 讓人確認程式有沒有正確理解輸入。
"""

from __future__ import annotations

from datetime import date

from usage_license_scraper.adapters import QueryType, registry
from usage_license_scraper.license_no import parse_license_no
from usage_license_scraper.matcher import AddressSpec, parse_address_specs
from usage_license_scraper.models import InputRow
from usage_license_scraper.normalize import normalize_license_text
from usage_license_scraper.storage import task_key

LEVEL_OK = "OK"
LEVEL_WARN = "警告"
LEVEL_ERROR = "錯誤"

# 已知各縣市資料來源的限制
# 來源資料缺漏的年度
_MISSING_YEARS = {"台北市": {"051": "台北市 051 年的開放資料缺漏（來源檔案有誤），這筆可能查無資料"}}

_DATA_LIMITS = {
    "新竹縣": (96, "新竹縣 opendata 只更新到民國 96 年左右，這筆要用 bupic 查：執行時需貼上瀏覽器查詢過的 JSESSIONID"),
}


def _format_spec(spec: AddressSpec) -> str:
    if spec.lo is None or spec.hi is None:
        return f"{spec.road}（整條）"

    def fmt(n: tuple[int, int]) -> str:
        return f"{n[0]}之{n[1]}" if n[1] else str(n[0])

    number = fmt(spec.lo) if spec.lo == spec.hi else f"{fmt(spec.lo)}~{fmt(spec.hi)}"
    return f"{spec.road} {number}號"


def normalized_rows(rows: list[InputRow]) -> list[dict[str, str]]:
    """正規化後的輸入檔，欄位跟主要輸入格式相同，另外附上解析出的年、號，可以直接拿來跑 batch。"""
    return [
        {
            "COMMUNITY_NO": row.community_id,
            "CASE_NAME": row.case_name,
            "CITY": row.city,
            "DIST": row.district,
            "ADDR_NO": row.address,
            "LICENSE_NO": normalize_license_text(row.license_text),
            "USE_FOR": row.use_for,
            "使用執照年": row.license_year,
            "使用執照號": row.license_number,
            "完整字號": row.full_license_key,
        }
        for row in rows
    ]


def check_rows(rows: list[InputRow]) -> list[dict[str, str]]:
    roc_year = date.today().year - 1911
    seen: dict[tuple, int] = {}
    results = []
    for i, row in enumerate(rows, start=2):  # 第 1 列是表頭，資料從第 2 列開始（跟 Excel 列號一致）
        errors: list[str] = []
        warnings: list[str] = []

        if not row.city:
            errors.append("缺少縣市")
        elif row.city not in registry:
            options = [c for c in (row.city + "市", row.city + "縣") if c in registry]
            hint = f"，請寫成{'或'.join(options)}" if options else ""
            errors.append(f"縣市「{row.city}」尚未支援{hint}")
        if row.input_city and row.input_city.replace("臺", "台").strip() != row.city:
            warnings.append(f"縣市「{row.input_city}」視為「{row.city}」")  # 舊縣名轉換才提醒，臺／台不算
        if not row.community_id:
            warnings.append("缺少社區編號")

        parsed = parse_license_no(row.license_text) if row.license_text else None
        if row.license_text and not parsed:
            adapter_cls = registry.get(row.city)
            if row.address and adapter_cls and QueryType.ADDRESS in adapter_cls.supported_queries:
                warnings.append("使用執照字號解析不出年份，只能用地址找候選，可能需要人工確認")
            else:
                errors.append("使用執照字號無法解析出年、號")
        elif not (row.license_year.isdigit() and row.license_number.isdigit()):
            adapter_cls = registry.get(row.city)
            if row.address and adapter_cls and QueryType.ADDRESS in adapter_cls.supported_queries:
                warnings.append("沒有使用執照字號，只能用地址找候選，可能需要人工確認")
            else:
                errors.append("缺少使用執照年或號")
        else:
            year = int(row.license_year)
            if year > roc_year:
                warnings.append(f"年份 {year} 晚於今年（民國 {roc_year} 年）")
            missing = _MISSING_YEARS.get(row.city, {}).get(str(year).zfill(3))
            if missing:
                warnings.append(missing)
            limit = _DATA_LIMITS.get(row.city)
            if limit and year > limit[0]:
                warnings.append(limit[1])

        specs = parse_address_specs(row.address, row.district) if row.address else []
        if not row.address:
            warnings.append("沒有地址，只能用年號＋行政區比對，同年號多張時會列入待確認")
        elif not specs:
            warnings.append("地址拆不出路名，無法用地址比對")
        elif any(s.lo is None for s in specs) and "全" not in row.address:
            warnings.append("地址有一段沒有門牌號，會比對整條路")

        key = task_key(row)
        if key in seen:
            warnings.append(f"跟第 {seen[key]} 列重複，只會查一次")
        else:
            seen[key] = i

        results.append({
            "列號": str(i),
            "COMMUNITY_NO": row.community_id,
            "CASE_NAME": row.case_name,
            "CITY": row.input_city or row.city,
            "正規化縣市": row.city,
            "DIST": row.district,
            "ADDR_NO": row.address,
            "地址解析": "；".join(_format_spec(s) for s in specs),
            "LICENSE_NO": row.license_text,
            "年": row.license_year,
            "字": parsed.word if parsed else "",
            "號": row.license_number,
            "完整字號": row.full_license_key,
            "檢查結果": LEVEL_ERROR if errors else (LEVEL_WARN if warnings else LEVEL_OK),
            "說明": "；".join(errors + warnings),
        })
    return results
