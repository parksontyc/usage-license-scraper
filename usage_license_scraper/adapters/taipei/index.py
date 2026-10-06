"""台北市使用執照本機索引（data/taipei_index.db）。

台北市的資料是一整份開放資料集，不是逐筆上網查，所以先把 XML 匯入成本機 SQLite 索引再查。
索引跟主資料庫（licenses.db）分開：licenses 只放比對成功的執照，台北市整份 8 萬多筆不放進去。

自動更新（ensure_ready）：
- config/taipei.toml 列出的年度，每個月下載一次 Taipei_<年度>.xml 到 XML 資料夾
- XML 資料夾的檔案有變動（新增、大小或修改時間不同）就重建索引
- 內容完全相同的檔案只匯入一次（例如 51.xml 跟 52.xml 是同一份）

來源資料裡同一個字號偶爾有兩筆：內容相同的只留一筆；內容不同的（多半是同一張執照、其中一筆
欄位缺漏）保留欄位比較完整的那一筆，並記下重複筆數（adapter 會在 source 欄加註）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tomllib
import zlib
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

import requests
import urllib3
from loguru import logger

from usage_license_scraper.adapters.taipei.parser import iter_records
from usage_license_scraper.models import AddressRecord, FloorRecord, LandRecord, LicenseRecord, ParkingRecord

DEFAULT_XML_DIR = Path("data/taipei_usage_license_refer")
DEFAULT_INDEX_PATH = Path("data/taipei_index.db")
CONFIG_PATH = Path("config/taipei.toml")
MAX_ROAD_RESULTS = 300  # 只給路名的門牌查詢最多回傳幾張（台北的大馬路動輒上千張）

urllib3.disable_warnings()  # data.taipei 的憑證鏈 Python 不認（curl 可以），同其他政府網站不驗證憑證

_CHILD_TYPES = {"addresses": AddressRecord, "lands": LandRecord, "floors": FloorRecord, "parkings": ParkingRecord}


def load_download_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    """{'115': '下載網址', ...}"""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        config = tomllib.load(f)
    return {str(year): url for year, url in (config.get("download") or {}).items()}


def _completeness(record: LicenseRecord) -> int:
    main = sum(1 for k, v in asdict(record).items() if k not in _CHILD_TYPES and v)
    return main + sum(len(getattr(record, k)) for k in _CHILD_TYPES)


def _pack(record: LicenseRecord) -> bytes:
    """空白欄位不存、壓縮後存：7 萬多筆、每筆動輒幾十個門牌，不壓縮索引會到 400 多 MB。"""
    data = {k: v for k, v in asdict(record).items() if v}
    for key in _CHILD_TYPES:
        if key in data:
            data[key] = [{k: v for k, v in item.items() if v} for item in data[key]]
    return zlib.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def record_from_json(packed: bytes) -> LicenseRecord:
    data = json.loads(zlib.decompress(packed).decode("utf-8"))
    for key, cls in _CHILD_TYPES.items():
        data[key] = [cls(**item) for item in data.get(key, [])]
    known = {f.name for f in fields(LicenseRecord)}
    return LicenseRecord(**{k: v for k, v in data.items() if k in known})


class TaipeiIndex:
    def __init__(self, index_path: Path = DEFAULT_INDEX_PATH, xml_dir: Path = DEFAULT_XML_DIR) -> None:
        self.xml_dir = Path(xml_dir)
        index_path = Path(index_path)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(index_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS licenses (
                license_key TEXT PRIMARY KEY, year TEXT, number TEXT, data BLOB,
                source_file TEXT, completeness INTEGER, duplicates INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS addresses (license_key TEXT, road TEXT, number TEXT);
            CREATE TABLE IF NOT EXISTS lands (license_key TEXT, section TEXT, land_number TEXT);
            CREATE INDEX IF NOT EXISTS ix_lic_year_no ON licenses (year, number);
            CREATE INDEX IF NOT EXISTS ix_addr_road ON addresses (road, number);
            CREATE INDEX IF NOT EXISTS ix_land_section ON lands (section);
            """
        )

    def close(self) -> None:
        self.conn.close()

    def _meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ── 更新 ──

    def ensure_ready(self, download: bool = True, force: bool = False) -> None:
        """需要的話下載當年度資料、重建索引。force=True 時不管本月是否下載過都重新下載並重建。"""
        if download:
            self.download(force=force)
        signature = self._signature()
        if force or signature != self._meta("signature"):
            self.rebuild(signature)

    def download(self, force: bool = False) -> None:
        month = date.today().strftime("%Y-%m")
        for year, url in load_download_config().items():
            target = self.xml_dir / f"Taipei_{year}.xml"
            if not force and target.exists() and self._meta(f"downloaded:{year}") == month:
                continue
            logger.info(f"[台北市] 下載 {year} 年使用執照資料…")
            try:
                resp = requests.get(url, timeout=120, verify=False)
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning(f"[台北市] {year} 年資料下載失敗（{type(e).__name__}），先使用現有檔案")
                continue
            if not resp.content.lstrip(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
                logger.warning(f"[台北市] {year} 年下載內容不是 XML，先使用現有檔案（請確認 config/taipei.toml 的網址）")
                continue
            self.xml_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".xml.tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(target)
            with self.conn:
                self._set_meta(f"downloaded:{year}", month)

    def _xml_files(self) -> list[Path]:
        return sorted(self.xml_dir.glob("*.xml"))

    def _signature(self) -> str:
        return json.dumps([(p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in self._xml_files()])

    def rebuild(self, signature: str | None = None) -> None:
        files = self._xml_files()
        if not files:
            raise RuntimeError(f"找不到台北市使用執照資料：{self.xml_dir} 底下沒有 XML 檔")
        logger.info(f"[台北市] 重建本機索引（{len(files)} 個檔案，第一次約需 1～2 分鐘）…")
        seen_hashes: dict[str, str] = {}
        kept: dict[str, tuple[LicenseRecord, str, int]] = {}
        duplicates: dict[str, int] = {}
        for path in files:
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            if digest in seen_hashes:
                logger.warning(f"[台北市] {path.name} 跟 {seen_hashes[digest]} 內容完全相同，略過")
                continue
            seen_hashes[digest] = path.name
            for record in iter_records(path):
                key = record.license_key
                score = _completeness(record)
                if key in kept:
                    duplicates[key] = duplicates.get(key, 1) + (asdict(kept[key][0]) != asdict(record))
                    if score <= kept[key][2]:
                        continue
                kept[key] = (record, path.name, score)

        with self.conn:
            for table in ("licenses", "addresses", "lands"):
                self.conn.execute(f"DELETE FROM {table}")
            self._insert(kept, duplicates)
            self._set_meta("signature", signature or self._signature())
        self.conn.execute("VACUUM")  # 重建後釋放舊資料佔的空間
        differ = sum(1 for v in duplicates.values() if v > 1)
        logger.info(f"[台北市] 索引完成：{len(kept)} 張執照（來源重複字號中，內容不同的 {differ} 組已保留較完整的一筆）")

    def _insert(self, kept: dict[str, tuple[LicenseRecord, str, int]], duplicates: dict[str, int]) -> None:
        from usage_license_scraper.matcher import parse_address  # 避免 adapters ↔ matcher 循環 import

        for key, (record, source_file, score) in kept.items():
            self.conn.execute(
                "INSERT INTO licenses (license_key, year, number, data, source_file, completeness, duplicates) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, record.license_year, record.license_number, _pack(record),
                 source_file, score, duplicates.get(key, 1)),
            )
            roads = set()
            for a in record.addresses:
                p = parse_address(a.full_address)
                if p and p.road:
                    roads.add((p.road, p.number.split("-")[0]))
            self.conn.executemany("INSERT INTO addresses VALUES (?, ?, ?)", [(key, r, n) for r, n in roads])
            self.conn.executemany(
                "INSERT INTO lands VALUES (?, ?, ?)", [(key, l.section, l.land_number) for l in record.lands]
            )

    # ── 查詢 ──

    def _records(self, keys: list[str]) -> list[tuple[LicenseRecord, int]]:
        out = []
        for key in dict.fromkeys(keys):
            r = self.conn.execute("SELECT data, duplicates FROM licenses WHERE license_key = ?", (key,)).fetchone()
            if r:
                out.append((record_from_json(r["data"]), r["duplicates"]))
        return out

    def by_number(self, year: str, number: str) -> list[tuple[LicenseRecord, int]]:
        rows = self.conn.execute(
            "SELECT license_key FROM licenses WHERE year = ? AND number = ?", (year.zfill(3), str(int(number)))
        ).fetchall()
        return self._records([r["license_key"] for r in rows])

    def by_key(self, key: str) -> list[tuple[LicenseRecord, int]]:
        return self._records([key])

    def by_year(self, year: str) -> list[tuple[LicenseRecord, int]]:
        rows = self.conn.execute("SELECT license_key FROM licenses WHERE year = ?", (year.zfill(3),)).fetchall()
        return self._records([r["license_key"] for r in rows])

    def by_address(self, road: str, number: str = "") -> list[tuple[LicenseRecord, int]]:
        main = number.split("-")[0]
        if main:
            rows = self.conn.execute(
                "SELECT DISTINCT license_key FROM addresses WHERE road = ? AND number = ?", (road, main)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT license_key FROM addresses WHERE road = ? LIMIT ?", (road, MAX_ROAD_RESULTS)
            ).fetchall()
        return self._records([r["license_key"] for r in rows])

    def by_land(self, section: str, main_no: str = "", sub_no: str = "") -> list[tuple[LicenseRecord, int]]:
        rows = self.conn.execute(
            "SELECT DISTINCT license_key, land_number FROM lands WHERE section LIKE ?", (f"%{section}%",)
        ).fetchall()

        def ok(land_number: str) -> bool:
            m, _, s = land_number.partition("-")
            return (not main_no or m.lstrip("0") == main_no.lstrip("0")) and (
                not sub_no or s.lstrip("0") == sub_no.lstrip("0")
            )

        return self._records([r["license_key"] for r in rows if ok(r["land_number"])])
