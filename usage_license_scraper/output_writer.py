"""從資料庫匯出 CSV：
    license_main.csv     每筆執照一列的主要欄位（含 source 資料來源、fetched_at 查詢時間）
    license_address.csv  門牌明細（一執照對多列，含行政區）
    license_land.csv     地段地號明細（含行政區）
    community_license.csv  每一列批次輸入一列（欄位同輸入檔），附上對到的執照字號、比對方式、狀態，
                         以及重複提示：DUP_SEQ／DUP_COUNT（幾個社區對到同一張執照）、
                         COMMUNITY_ROWS（同一個社區在輸入檔出現幾列）
    pending.csv          待確認：同一列輸入對到多張執照、或地址與年號矛盾，列出所有候選供人工確認
    errors.csv           查無資料／查詢失敗的紀錄與原因

樓層（license_floor）、停車（license_parking）明細只存在資料庫，不匯出 CSV。
明細表用 (city, license_key) 對應回 license_main。
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from usage_license_scraper.storage import MAIN_COLUMNS, META_COLUMNS, STATUS_OK, STATUS_PENDING, LicenseStore

# 資料表 -> (CSV 檔名, 欄位順序)
_CHILD_EXPORTS = {
    "license_address": ("license_address.csv", ["city", "district", "license_key", "seq", "full_address"]),
    "license_land": ("license_land.csv", ["city", "district", "license_key", "seq", "section", "land_number", "area"]),
}

# pending.csv 給人看的，表頭用中文：資料庫欄位 -> 表頭
_PENDING_HEADERS = {
    "community_id": "社區編號",
    "case_name": "案名",
    "city": "縣市",
    "input_district": "行政區",
    "address": "代表號地址",
    "license_year": "使用執照年",
    "license_number": "使用執照號",
    "license_text": "使用執照字號（輸入）",
    "reason": "待確認原因",
    "candidate_key": "候選完整字號",
    "candidate_issue_date": "候選發照日期",
    "candidate_district": "候選行政區",
    "candidate_address": "候選門牌",
    "candidate_builder": "候選起造人",
    "candidate_original_license": "候選原領執照",
}

# community_license.csv：欄位跟主要輸入格式一致，後面接比對結果
_COMMUNITY_HEADERS = {
    "community_id": "COMMUNITY_NO",
    "case_name": "CASE_NAME",
    "city": "CITY",
    "district": "DIST",
    "address": "ADDR_NO",
    "license_text": "LICENSE_NO",
    "use_for": "USE_FOR",
    "status": "STATUS",
    "license_key": "MATCHED_LICENSE_KEY",
    "matched_by": "MATCHED_BY",
    "error": "NOTE",
}
# 匯出時計算的重複提示欄，接在 _COMMUNITY_HEADERS 後面。
# 用兩個數字欄而不是「1/2」這種寫法：CSV 用 Excel 開時「1/2」會被自動轉成日期。
_DUP_HEADERS = ["DUP_SEQ", "DUP_COUNT", "COMMUNITY_ROWS"]

# 以前會匯出、現在只存在資料庫的檔案；匯出時順手刪掉，避免留著過期的舊檔
_RETIRED_FILES = ["license_floor.csv", "license_parking.csv"]


def export_csv(store: LicenseStore, output_dir: str | Path, city: str | None = None) -> dict[str, int]:
    """匯出資料庫內容，city 有給的話只匯出該縣市。回傳各檔案的筆數。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    where, params = ("WHERE city = ?", (city,)) if city else ("", ())
    counts: dict[str, int] = {}

    rows = store.query(
        f"SELECT * FROM licenses {where} "
        "ORDER BY city, license_year, CAST(license_number AS INTEGER), license_key",
        params,
    )
    tasks = store.query(
        f"SELECT * FROM license_tasks {where} ORDER BY city, community_id, license_year, address", params
    )
    shared = _shared_licenses(tasks)

    main_cols = list(MAIN_COLUMNS)
    main_cols.insert(main_cols.index("use_for") + 1, "community_count")
    counts["license_main.csv"] = _write_csv(
        output_dir / "license_main.csv",
        main_cols + META_COLUMNS,
        [dict(r) | {"community_count": len(shared.get((r["city"], r["license_key"]), []))} for r in rows],
    )

    for table, (filename, cols) in _CHILD_EXPORTS.items():
        rows = store.query(f'SELECT * FROM "{table}" {where} ORDER BY city, license_key, CAST(seq AS INTEGER)', params)
        counts[filename] = _write_csv(output_dir / filename, cols, rows)

    community_rows: dict[tuple[str, str], int] = defaultdict(int)
    for t in tasks:
        if t["community_id"]:
            community_rows[(t["city"], t["community_id"])] += 1
    out_rows = []
    for t in tasks:
        row = {_COMMUNITY_HEADERS[k]: t[k] for k in _COMMUNITY_HEADERS}
        row |= {"CITY": t["input_city"] or t["city"], "ADDR_NO": t["input_address"] or t["address"]}
        group = shared.get((t["city"], t["license_key"])) if t["status"] == STATUS_OK else None
        row["DUP_SEQ"] = group.index(_task_id(t)) + 1 if group else ""
        row["DUP_COUNT"] = len(group) if group else ""
        row["COMMUNITY_ROWS"] = community_rows.get((t["city"], t["community_id"]), "")
        out_rows.append(row)
    counts["community_license.csv"] = _write_csv(
        output_dir / "community_license.csv", list(_COMMUNITY_HEADERS.values()) + _DUP_HEADERS, out_rows
    )

    rows = store.query(
        f"SELECT * FROM pending_candidates {where} "
        "ORDER BY city, license_year, CAST(license_number AS INTEGER), candidate_key",
        params,
    )
    counts["pending.csv"] = _write_csv(
        output_dir / "pending.csv",
        list(_PENDING_HEADERS.values()),
        [{_PENDING_HEADERS[k]: r[k] for k in _PENDING_HEADERS} for r in rows],
    )

    err_where = f"{where} AND status NOT IN (?, ?)" if where else "WHERE status NOT IN (?, ?)"
    rows = store.query(
        f"SELECT * FROM license_tasks {err_where} ORDER BY city, license_year, CAST(license_number AS INTEGER)",
        (*params, STATUS_OK, STATUS_PENDING),
    )
    counts["errors.csv"] = _write_csv(
        output_dir / "errors.csv",
        ["community_id", "case_name", "city", "license_year", "license_number", "status", "error", "updated_at"],
        rows,
    )

    for name in _RETIRED_FILES:
        (output_dir / name).unlink(missing_ok=True)
    return counts


def _task_id(task) -> tuple[str, ...]:
    return (task["city"], task["license_year"], task["license_number"], task["community_id"], task["address"])


def _shared_licenses(tasks) -> dict[tuple[str, str], list[tuple[str, ...]]]:
    """(縣市, 執照字號) -> 對到這張執照的批次輸入列（依社區編號排序，序號才會固定）。只算比對成功的。"""
    groups: dict[tuple[str, str], list[tuple[str, ...]]] = defaultdict(list)
    for t in tasks:
        if t["status"] == STATUS_OK and t["license_key"]:
            groups[(t["city"], t["license_key"])].append(_task_id(t))
    return {k: sorted(v, key=lambda x: (x[3], x[4], x[1], x[2])) for k, v in groups.items()}


def _write_csv(path: Path, fieldnames: list[str], rows) -> int:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    return len(rows)
