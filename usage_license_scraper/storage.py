"""SQLite 累積儲存：所有縣市的查詢結果都累積在同一個資料庫檔（預設 data/licenses.db）。

資料表：
    licenses          每張執照一列，主鍵 (city, license_key)；重複查到同一張執照會更新而不是重複新增
    license_address   門牌明細       ┐
    license_land      地號明細       │ 主鍵 (city, license_key, seq)；
    license_floor     樓層明細       │ 更新執照時整批刪掉重寫
    license_parking   停車明細       ┘
    license_tasks     每一列批次輸入的查詢狀態（ok／not_found／pending／error），用來跳過已查過的；
                      以「縣市＋年＋號＋社區編號＋代表號地址」辨識，同一個年號不同社區是不同列
    pending_candidates  狀態為 pending（待確認）的輸入列所對到的候選執照，匯出成 pending.csv；
                      候選不寫進 licenses，人工把字號填回輸入檔的「完整字號」後重跑才會存進去
    search_tasks      整年度／門牌／地號查詢的執行紀錄，用來跳過已跑過的查詢

不重複跑與續跑：每查完一筆就在同一個 transaction 裡寫入執照資料並標記任務狀態，
中途中斷（Ctrl+C、斷網、關機）時已完成的都已經寫進去了；重新執行同一個指令，
狀態是 ok／not_found／pending 的會直接跳過，只有 error（連線逾時等暫時性錯誤）會重試；
pending 的列在輸入檔填了「完整字號」之後會重新處理。

欄位由 models.py 的 dataclass 產生；之後 LicenseRecord 新增欄位，開啟資料庫時
會自動 ALTER TABLE 補上欄位，不需要手動改 schema。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from usage_license_scraper.district import district_from_address
from usage_license_scraper.normalize import normalize_address
from usage_license_scraper.models import (
    AddressRecord,
    FloorRecord,
    InputRow,
    LandRecord,
    LicenseRecord,
    ParkingRecord,
)

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"

CHILD_ATTRS = {
    # LicenseRecord 屬性 -> (資料表, 明細 dataclass)
    "addresses": ("license_address", AddressRecord),
    "lands": ("license_land", LandRecord),
    "floors": ("license_floor", FloorRecord),
    "parkings": ("license_parking", ParkingRecord),
}

MAIN_COLUMNS = [f.name for f in fields(LicenseRecord) if f.name not in CHILD_ATTRS]
META_COLUMNS = ["source", "matched_by", "fetched_at"]


TASK_KEY_COLUMNS = ["city", "license_year", "license_number", "community_id", "address"]
TASK_INFO_COLUMNS = ["district", "case_name", "license_text", "use_for", "input_city", "input_address"]  # 輸入列的其他欄位，原樣記下

PENDING_COLUMNS = [
    "city", "license_year", "license_number", "community_id", "address", "candidate_key",
    "input_district", "case_name", "license_text", "reason",
    "candidate_issue_date", "candidate_district", "candidate_address", "candidate_builder",
    "candidate_original_license", "updated_at",
]


def child_columns(cls: type) -> list[str]:
    return [f.name for f in fields(cls) if f.name != "license_key"]


def task_key(row: InputRow) -> tuple[str, str, str, str, str]:
    """一列批次輸入的識別：縣市＋年＋號＋社區編號＋代表號地址。

    年號不唯一（例如桃園 75 年第 1 號有 9 張），不同社區可能是同一個年號，
    所以要連社區編號、地址一起當識別。年號寫法統一：'1'／'00001'、'95'／'095' 視為同一筆。
    """
    year = row.license_year.strip()
    number = row.license_number.strip()
    return (
        row.city.strip(),
        year.zfill(3) if year.isdigit() else year,
        str(int(number)) if number.isdigit() else number,
        row.community_id.strip(),
        row.address.strip(),
    )


_TASK_WHERE = " AND ".join(f'"{c}" = ?' for c in TASK_KEY_COLUMNS)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LicenseStore:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    # ── schema ──

    def _init_schema(self) -> None:
        main_cols = MAIN_COLUMNS + META_COLUMNS
        with self.conn:
            self._ensure_table("licenses", main_cols, "city, license_key")
            for table, cls in CHILD_ATTRS.values():
                self._ensure_table(table, ["city", "license_key", *child_columns(cls)], "city, license_key, seq")
            self._ensure_table(
                "license_tasks",
                [*TASK_KEY_COLUMNS, *TASK_INFO_COLUMNS, "status", "license_key", "matched_by", "error", "updated_at"],
                ", ".join(TASK_KEY_COLUMNS),
            )
            self._ensure_table(
                "search_tasks",
                ["city", "query_type", "params", "status", "result_count", "error", "updated_at"],
                "city, query_type, params",
            )
            self._ensure_table("pending_candidates", PENDING_COLUMNS, ", ".join([*TASK_KEY_COLUMNS, "candidate_key"]))
            self._backfill_districts()
            self._normalize_task_addresses()
            # case_name、use_for 是後來加的：舊的執照從批次輸入紀錄補上
            for col in ("case_name", "use_for"):
                self.conn.execute(
                    f"UPDATE licenses SET {col} = COALESCE((SELECT t.{col} FROM license_tasks t "
                    f"WHERE t.city = licenses.city AND t.license_key = licenses.license_key AND t.{col} != '' LIMIT 1), '') "
                    f"WHERE {col} = ''"
                )

    def _normalize_task_addresses(self) -> None:
        """地址改成讀檔時正規化之後，舊紀錄的地址（也是「查過了沒」的識別）一併正規化，
        原始寫法搬到 input_address；正規化後跟既有紀錄撞在一起的，保留既有的那筆。"""
        self.conn.create_function("norm_addr", 1, normalize_address, deterministic=True)
        for table in ("license_tasks", "pending_candidates"):
            if table == "license_tasks":
                self.conn.execute("UPDATE license_tasks SET input_address = address WHERE input_address = '' AND address != ''")
            self.conn.execute(f"UPDATE OR IGNORE {table} SET address = norm_addr(address) WHERE address != norm_addr(address)")
            self.conn.execute(f"DELETE FROM {table} WHERE address != norm_addr(address)")

    def _backfill_districts(self) -> None:
        """district 欄位是後來加的，舊資料從門牌／地段補上。"""
        for table, source_col in (("license_address", "full_address"), ("license_land", "section")):
            rows = self.conn.execute(f'SELECT rowid, "{source_col}" AS src FROM "{table}" WHERE district = \'\'').fetchall()
            self.conn.executemany(
                f'UPDATE "{table}" SET district = ? WHERE rowid = ?',
                [(district_from_address(r["src"]), r["rowid"]) for r in rows if district_from_address(r["src"])],
            )

    def _ensure_table(self, table: str, columns: list[str], primary_key: str) -> None:
        col_defs = ", ".join(f'"{c}" TEXT NOT NULL DEFAULT \'\'' for c in columns)
        create_sql = f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs}, PRIMARY KEY ({primary_key}))'
        info = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        current_pk = [r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])]
        wanted_pk = [c.strip() for c in primary_key.split(",")]
        if info and current_pk != wanted_pk:
            # 主鍵改了（SQLite 不能直接改主鍵）：建新表、搬資料、刪舊表
            old = f"{table}__old"
            self.conn.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
            self.conn.execute(create_sql)
            common = [r["name"] for r in info if r["name"] in columns]
            cols = ", ".join(f'"{c}"' for c in common)
            self.conn.execute(f'INSERT OR IGNORE INTO "{table}" ({cols}) SELECT {cols} FROM "{old}"')
            self.conn.execute(f'DROP TABLE "{old}"')
        self.conn.execute(create_sql)
        existing = {r["name"] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
        for c in columns:
            if c not in existing:
                self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" TEXT NOT NULL DEFAULT \'\'')

    # ── 執照資料 ──

    def _upsert_license(self, record: LicenseRecord, source: str, matched_by: str = "") -> str:
        data = {k: v for k, v in asdict(record).items() if k in MAIN_COLUMNS}
        if not data["license_key"]:
            data["license_key"] = f"{record.license_year}-{record.license_number}"
        data.update(source=source, matched_by=matched_by, fetched_at=_now())
        cols = list(data)
        # community_id／district：新的值是空的就保留原本的（例如整年度查詢沒有社區編號）
        keep_if_empty = {"community_id", "case_name", "use_for", "district"}
        updates = ", ".join(
            f'"{c}" = COALESCE(NULLIF(excluded."{c}", \'\'), licenses."{c}")' if c in keep_if_empty else f'"{c}" = excluded."{c}"'
            for c in cols
            if c not in ("city", "license_key")
        )
        col_list = ", ".join(f'"{c}"' for c in cols)
        self.conn.execute(
            f"INSERT INTO licenses ({col_list}) "
            f'VALUES ({", ".join("?" for _ in cols)}) '
            f"ON CONFLICT (city, license_key) DO UPDATE SET {updates}",
            [data[c] for c in cols],
        )

        key = data["license_key"]
        for item in record.addresses:
            item.district = item.district or district_from_address(item.full_address)
        for item in record.lands:
            item.district = item.district or district_from_address(item.section)
        for attr, (table, cls) in CHILD_ATTRS.items():
            self.conn.execute(f'DELETE FROM "{table}" WHERE city = ? AND license_key = ?', (record.city, key))
            ccols = child_columns(cls)
            rows = [[record.city, key, *(getattr(item, c) for c in ccols)] for item in getattr(record, attr)]
            if rows:
                placeholders = ", ".join("?" for _ in range(len(ccols) + 2))
                col_list = ", ".join(f'"{c}"' for c in ["city", "license_key", *ccols])
                # 同一張執照的明細若 seq 重複（來源資料本身重複），以後面的為準
                self.conn.executemany(f'INSERT OR REPLACE INTO "{table}" ({col_list}) VALUES ({placeholders})', rows)
        return key

    def _mark_task(self, row: InputRow, status: str, license_key: str = "", error: str = "", matched_by: str = "") -> None:
        key = task_key(row)
        cols = [*TASK_KEY_COLUMNS, *TASK_INFO_COLUMNS, "status", "license_key", "matched_by", "error", "updated_at"]
        updates = ", ".join(f'"{c}" = excluded."{c}"' for c in cols if c not in TASK_KEY_COLUMNS)
        self.conn.execute(
            f"INSERT INTO license_tasks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT ({', '.join(TASK_KEY_COLUMNS)}) DO UPDATE SET {updates}",
            (*key, row.district, row.case_name, row.license_text, row.use_for, row.input_city, row.input_address,
             status, license_key, matched_by, error, _now()),
        )
        if status != STATUS_PENDING:
            self.conn.execute(f"DELETE FROM pending_candidates WHERE {_TASK_WHERE}", key)
        city, year, number, community_id, address = key
        if community_id:
            # 同一個社區改了使用執照字號（年或號變了）：舊年號的那筆紀錄作廢，避免重複
            stale = "city = ? AND community_id = ? AND address = ? AND NOT (license_year = ? AND license_number = ?)"
            for table in ("license_tasks", "pending_candidates"):
                self.conn.execute(f"DELETE FROM {table} WHERE {stale}", (city, community_id, address, year, number))

    def save_success(self, row: InputRow, record: LicenseRecord, source: str, matched_by: str = "") -> None:
        with self.conn:
            key = self._upsert_license(record, source, matched_by)
            self._mark_task(row, STATUS_OK, license_key=key, matched_by=matched_by)

    def save_failure(self, row: InputRow, status: str, error: str) -> None:
        with self.conn:
            self._mark_task(row, status, error=error)

    def save_pending(self, row: InputRow, reason: str, candidates: list[LicenseRecord]) -> None:
        """待確認：只記候選，不寫進 licenses。"""
        key = task_key(row)
        now = _now()
        with self.conn:
            self._mark_task(row, STATUS_PENDING, error=reason)
            self.conn.execute(f"DELETE FROM pending_candidates WHERE {_TASK_WHERE}", key)
            for c in candidates:
                addresses = "、".join(a.full_address for a in c.addresses[:3])
                if len(c.addresses) > 3:
                    addresses += f"…等 {len(c.addresses)} 筆"
                values = {
                    **dict(zip(TASK_KEY_COLUMNS, key)), "candidate_key": c.license_key,
                    "input_district": row.district, "case_name": row.case_name, "license_text": row.license_text,
                    "reason": reason, "candidate_issue_date": c.issue_date,
                    "candidate_district": c.district or (c.addresses[0].district if c.addresses else ""),
                    "candidate_address": addresses, "candidate_builder": c.builder,
                    "candidate_original_license": c.original_license, "updated_at": now,
                }
                cols = ", ".join(f'"{k}"' for k in values)
                self.conn.execute(
                    f"INSERT OR REPLACE INTO pending_candidates ({cols}) VALUES ({', '.join('?' for _ in values)})",
                    list(values.values()),
                )

    def task_info(self, row: InputRow) -> sqlite3.Row | None:
        """這列輸入上次的處理結果（status、license_key、license_text…）；沒查過回傳 None。"""
        return self.conn.execute(f"SELECT * FROM license_tasks WHERE {_TASK_WHERE}", task_key(row)).fetchone()

    # ── 整年度／門牌／地號查詢 ──

    def search_status(self, city: str, query_type: str, params: str) -> str | None:
        r = self.conn.execute(
            "SELECT status FROM search_tasks WHERE city = ? AND query_type = ? AND params = ?", (city, query_type, params)
        ).fetchone()
        return r["status"] if r else None

    def save_search(self, city: str, query_type: str, params: str, records: list[LicenseRecord], source: str) -> None:
        """整批寫入查詢結果。不寫 license_tasks：那是批次輸入列的紀錄，這類查詢沒有輸入列。"""
        with self.conn:
            for record in records:
                self._upsert_license(record, source, matched_by=f"{query_type}查詢")
            self._mark_search(city, query_type, params, STATUS_OK, len(records))

    def save_search_failure(self, city: str, query_type: str, params: str, error: str) -> None:
        with self.conn:
            self._mark_search(city, query_type, params, STATUS_ERROR, 0, error)

    def _mark_search(self, city: str, query_type: str, params: str, status: str, count: int, error: str = "") -> None:
        self.conn.execute(
            "INSERT INTO search_tasks (city, query_type, params, status, result_count, error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (city, query_type, params) DO UPDATE SET "
            "status = excluded.status, result_count = excluded.result_count, error = excluded.error, updated_at = excluded.updated_at",
            (city, query_type, params, status, str(count), error, _now()),
        )

    # ── 匯出用 ──

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def summary(self) -> list[sqlite3.Row]:
        """各縣市的執照數，以及批次輸入列的狀態統計（整年度等查詢存進來的縣市也會列出）。"""
        return self.query(
            "SELECT c.city, "
            "(SELECT COUNT(*) FROM licenses l WHERE l.city = c.city) AS licenses, "
            "COALESCE(SUM(t.status = 'ok'), 0) AS ok, COALESCE(SUM(t.status = 'not_found'), 0) AS not_found, "
            "COALESCE(SUM(t.status = 'pending'), 0) AS pending, COALESCE(SUM(t.status = 'error'), 0) AS error "
            "FROM (SELECT city FROM licenses UNION SELECT city FROM license_tasks) c "
            "LEFT JOIN license_tasks t ON t.city = c.city GROUP BY c.city ORDER BY c.city"
        )
