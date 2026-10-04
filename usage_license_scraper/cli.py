"""查詢入口，每種查詢方式一個子指令。查詢結果累積寫進 SQLite（預設 data/licenses.db），
查完自動把整個資料庫匯出成 CSV（預設 data/output，加 --no-export 可略過）：

    # 整批查詢：一份 CSV/Excel 裡列出的執照（所有縣市）
    python -m usage_license_scraper batch --input data/input/batch.csv

    # 使用執照號：查單筆（所有縣市）
    python -m usage_license_scraper license --city 新北市 --year 115 --number 1

    # 整年度（opendata 縣市：桃園市／新竹市／台中市／高雄市）
    python -m usage_license_scraper year --city 桃園市 --year 115

    # 門牌（opendata 縣市）
    python -m usage_license_scraper address --city 桃園市 --district 八德區 --road 永豐路519巷2弄 --number 7

    # 地號（opendata 縣市）
    python -m usage_license_scraper land --city 桃園市 --section 高明段 --main-no 990

    # 手動匯出 CSV（例如只匯出某個縣市）、看累積進度
    python -m usage_license_scraper export --output data/output
    python -m usage_license_scraper status

不重複跑／續跑：已經查過的（成功或查無資料）預設跳過，每查完一筆就寫進資料庫，
中斷後重新執行同一個指令就會從沒查過的繼續。--refresh 強制重查，
--retry-not-found 重查之前查無資料的。整年度查詢的年份若是今年，因為資料還會
持續增加，不會被跳過。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path

from loguru import logger

from usage_license_scraper.adapters import CityAdapter, QueryType, cities_supporting, registry
from usage_license_scraper.input_check import LEVEL_ERROR, LEVEL_WARN, check_rows, normalized_rows
from usage_license_scraper.input_loader import load_input_rows
from usage_license_scraper.tainan_probe import pending_targets, run_probe
from usage_license_scraper.matcher import NO_MATCH_MARK, match_license, normalize_key
from usage_license_scraper.models import AddressQuery, InputRow, LandQuery, LicenseRecord
from usage_license_scraper.output_writer import export_csv
from usage_license_scraper.storage import (
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_PENDING,
    LicenseStore,
    task_key,
)

DEFAULT_DB = "data/licenses.db"
DEFAULT_OUTPUT = "data/output"
DEFAULT_CHECK_OUTPUT = "data/input/normalized"
QUERY_COMMANDS = {"batch", "license", "year", "address", "land"}


def _current_roc_year() -> str:
    return str(date.today().year - 1911).zfill(3)


def _get_adapter_cls(city: str, query: QueryType) -> type[CityAdapter]:
    adapter_cls = registry.get(city)
    if adapter_cls is None:
        raise SystemExit(f"[{city}] 尚未支援此縣市，目前支援：{'、'.join(registry)}")
    if query not in adapter_cls.supported_queries:
        raise SystemExit(
            f"[{city}] 不支援「{query.value}」查詢，支援此查詢的縣市：{'、'.join(cities_supporting(query))}"
        )
    return adapter_cls


# ── 使用執照號（整批／單筆） ──


def _should_skip(
    store: LicenseStore, row: InputRow, refresh: bool, retry_not_found: bool, retry_pending: bool = False
) -> bool:
    if refresh:
        return False
    info = store.task_info(row)
    if info is None:
        return False
    status, stored_key = info["status"], info["license_key"]
    full_key = normalize_key(row.full_license_key)
    if full_key:
        # 填了完整字號：已經照這個字號存過（或已確認為「無」）才跳過，否則重新處理
        return (status == STATUS_OK and normalize_key(stored_key) == full_key) or (
            status == STATUS_NOT_FOUND and full_key == NO_MATCH_MARK
        )
    if status == STATUS_PENDING:
        # 待確認的列：輸入檔的使用執照字號改過（例如貼上 pending.csv 的候選字號）就重新處理
        return not retry_pending and normalize_key(info["license_text"]) == normalize_key(row.license_text)
    if status == STATUS_OK:
        return True
    return status == STATUS_NOT_FOUND and not retry_not_found


def _pending_rows(
    store: LicenseStore, rows: list[InputRow], refresh: bool, retry_not_found: bool, retry_pending: bool = False
) -> list[InputRow]:
    """去掉已經查過的、以及輸入檔裡重複的列。"""
    todo: list[InputRow] = []
    seen: set[tuple[str, str, str]] = set()
    skipped = duplicated = 0
    for row in rows:
        key = task_key(row)
        if key in seen:
            duplicated += 1
            continue
        seen.add(key)
        if _should_skip(store, row, refresh, retry_not_found, retry_pending):
            skipped += 1
            continue
        todo.append(row)
    if skipped:
        logger.info(f"跳過 {skipped} 筆已查過的（要重查請加 --refresh 或 --retry-not-found）")
    if duplicated:
        logger.info(f"輸入檔有 {duplicated} 筆重複，只查一次")
    return todo


def _fetch_rows(store: LicenseStore, adapter_cls: type[CityAdapter], rows: list[InputRow]) -> int:
    """逐筆比對並存檔，回傳待確認的筆數。"""
    pending = 0
    adapter = adapter_cls()
    try:
        if adapter.requires_captcha:
            try:
                adapter.open_session()
            except RuntimeError as e:  # 驗證碼一直輸入錯誤：這個縣市先跳過，下次執行再查
                logger.error(f"[{adapter.city_name}] {e}，這次先跳過 {len(rows)} 筆，下次執行會再查")
                return 0
        for i, row in enumerate(rows, start=1):
            who = f"{row.community_id} " if row.community_id else ""
            label = f"  [{i}/{len(rows)}] {who}{row.license_year}-{row.license_number}"
            try:
                result = match_license(adapter, row)
            except Exception as e:  # noqa: BLE001 - 單筆失敗不應中斷整批，下次執行會自動重試
                store.save_failure(row, STATUS_ERROR, f"{type(e).__name__}: {e}")
                logger.warning(f"{label} 失敗（下次執行會重試）：{e}")
                continue
            if result.status == STATUS_OK:
                store.save_success(row, result.record, adapter.record_source(result.record), result.matched_by)
                logger.info(f"{label} ✓ {result.record.license_key}（{result.matched_by}）")
            elif result.status == STATUS_PENDING:
                pending += 1
                store.save_pending(row, result.reason, result.candidates)
                logger.warning(f"{label} 待確認：{result.reason}，候選 {len(result.candidates)} 張")
            else:
                store.save_failure(row, STATUS_NOT_FOUND, result.reason)
                logger.warning(f"{label} {result.reason}")
    finally:
        adapter.close()
    return pending


def _log_pending_hint(pending: int) -> None:
    if pending:
        logger.warning(
            f"有 {pending} 筆待確認，候選執照列在 pending.csv。確認後把字號填進輸入檔的「完整字號」欄"
            f"（都不對就填「{NO_MATCH_MARK}」），再執行一次同樣的指令即可存入。"
        )


def run_batch(
    store: LicenseStore, input_path: str, refresh: bool, retry_not_found: bool, retry_pending: bool = False
) -> None:
    rows = load_input_rows(input_path)
    logger.info(f"讀入 {len(rows)} 筆查詢條件")
    rows = _pending_rows(store, rows, refresh, retry_not_found, retry_pending)

    by_city: dict[str, list[InputRow]] = defaultdict(list)
    for row in rows:
        by_city[row.city].append(row)

    pending = 0
    for city, city_rows in by_city.items():
        adapter_cls = registry.get(city)
        if adapter_cls is None or QueryType.LICENSE_NUMBER not in adapter_cls.supported_queries:
            logger.warning(f"[{city}] 尚未支援此縣市，共 {len(city_rows)} 筆資料跳過")
            for r in city_rows:
                store.save_failure(r, STATUS_ERROR, "此縣市尚未支援")
            continue
        logger.info(f"[{city}] 開始查詢 {len(city_rows)} 筆")
        pending += _fetch_rows(store, adapter_cls, city_rows)

    _log_pending_hint(pending)



def run_license(
    store: LicenseStore, city: str, year: str, number: str, key: str, refresh: bool, retry_not_found: bool
) -> None:
    adapter_cls = _get_adapter_cls(city, QueryType.LICENSE_NUMBER)
    row = InputRow(
        community_id="", city=city, district="", address="", license_year=year, license_number=number,
        full_license_key=key,
    )
    if not _pending_rows(store, [row], refresh, retry_not_found):
        return
    logger.info(f"[{city}] 查詢 {year} 年第 {number} 號" + (f"（完整字號 {key}）" if key else ""))
    _log_pending_hint(_fetch_rows(store, adapter_cls, [row]))


# ── 回傳多筆的查詢（整年度／門牌／地號） ──


def _run_search(
    store: LicenseStore,
    city: str,
    query: QueryType,
    params: dict[str, str],
    refresh: bool,
    call: Callable[[CityAdapter], list[LicenseRecord]],
) -> None:
    adapter_cls = _get_adapter_cls(city, query)
    params_key = json.dumps(params, ensure_ascii=False, sort_keys=True)
    desc = f"{query.value}查詢 {params_key}"

    is_current_year = query is QueryType.YEAR and params["year"].zfill(3) == _current_roc_year()
    if not refresh and not is_current_year and store.search_status(city, query.value, params_key) == STATUS_OK:
        logger.info(f"[{city}] {desc} 已經跑過，跳過（要重跑請加 --refresh）")
        return
    if is_current_year:
        logger.info(f"[{city}] {params['year']} 年是今年，資料還會增加，照常查詢")

    logger.info(f"[{city}] {desc}（自動翻頁）")
    adapter = adapter_cls()
    try:
        if adapter.requires_captcha:
            adapter.open_session()
        records = [r for r in call(adapter) if not r.is_misc]  # 只收建築物使用執照
    except ValueError as e:  # 查詢條件不足等使用者輸入問題
        raise SystemExit(f"[{city}] {e}") from e
    except Exception as e:
        store.save_search_failure(city, query.value, params_key, f"{type(e).__name__}: {e}")
        raise
    finally:
        adapter.close()

    store.save_search(city, query.value, params_key, records, adapter_cls.source)
    logger.info(f"[{city}] 查到 {len(records)} 筆，已寫入資料庫")


# ── 匯出／狀態 ──


def run_export(store: LicenseStore, output_dir: str, city: str | None) -> None:
    counts = export_csv(store, output_dir, city)
    for name, n in counts.items():
        logger.info(f"  {name}: {n} 筆")
    logger.info(f"已匯出至 {output_dir}")


def run_check(input_path: str, output_dir: str) -> None:
    rows = load_input_rows(input_path)
    results = check_rows(rows)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "input_check.csv"
    normalized_path = out / "input_normalized.csv"
    for target, data in ((path, results), (normalized_path, normalized_rows(rows))):
        with open(target, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0]) if data else ["COMMUNITY_NO"])
            writer.writeheader()
            writer.writerows(data)

    errors = [r for r in results if r["檢查結果"] == LEVEL_ERROR]
    warnings = [r for r in results if r["檢查結果"] == LEVEL_WARN]
    logger.info(f"共 {len(results)} 列：正常 {len(results) - len(errors) - len(warnings)}、警告 {len(warnings)}、錯誤 {len(errors)}")
    for r in errors + warnings:
        log = logger.error if r["檢查結果"] == LEVEL_ERROR else logger.warning
        log(f"  第 {r['列號']} 列 {r['COMMUNITY_NO']} {r['LICENSE_NO']}：{r['說明']}")
    logger.info(f"每一列的解析結果寫在 {path}")
    logger.info(f"正規化後的輸入檔寫在 {normalized_path}（可以直接拿來跑 batch）")
    if errors:
        logger.info("「錯誤」的列跑批次時一定會失敗，建議先修正；「警告」的列可以照跑，但結果可能需要人工確認。")


def run_status(store: LicenseStore) -> None:
    rows = store.summary()
    if not rows:
        print("資料庫還沒有任何資料")
        return
    print(f"{'縣市':<6}{'執照數':>8}{'成功':>8}{'查無資料':>8}{'待確認':>8}{'失敗待重試':>10}")
    for r in rows:
        print(f"{r['city']:<6}{r['licenses']:>10}{r['ok']:>10}{r['not_found']:>10}{r['pending']:>10}{r['error']:>12}")
    searches = store.query("SELECT city, query_type, params, status, result_count, updated_at FROM search_tasks ORDER BY updated_at")
    if searches:
        print("\n已執行的整年度／門牌／地號查詢：")
        for s in searches:
            print(f"  {s['updated_at']}  {s['city']} {s['query_type']} {s['params']}  {s['status']} {s['result_count']} 筆")


# ── 指令列 ──

PROG = "python -m usage_license_scraper"


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """保留說明文字的換行，並把 argparse 預設的英文標題換成中文。"""

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix or "用法：")


def _new_parser(**kwargs) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(formatter_class=_HelpFormatter, add_help=False, **kwargs)
    parser._positionals.title = "指令"
    parser._optionals.title = "選項"
    parser.add_argument("-h", "--help", action="help", help="顯示這份說明")
    return parser


def _add_command(sub, name: str, summary: str, description: str, examples: list[str]) -> argparse.ArgumentParser:
    epilog = "範例：\n" + "\n".join(f"  {PROG} {e}" for e in examples)
    p = sub.add_parser(
        name, prog=f"{PROG} {name}", help=summary, description=f"{summary}\n\n{description}".rstrip(), epilog=epilog,
        formatter_class=_HelpFormatter, add_help=False,
    )
    p._optionals.title = "選項"
    p.add_argument("-h", "--help", action="help", help="顯示這份說明")
    return p


def _add_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, metavar="FILE", help=f"資料庫檔案（預設 {DEFAULT_DB}）")


def _add_output(p: argparse.ArgumentParser) -> None:
    p.add_argument("--output", default=DEFAULT_OUTPUT, metavar="DIR", help=f"查完自動匯出 CSV 的目錄（預設 {DEFAULT_OUTPUT}）")
    p.add_argument("--no-export", action="store_true", help="查完不要自動匯出 CSV（資料量大、只想累積進資料庫時使用）")


def _add_save_html(p: argparse.ArgumentParser) -> None:
    p.add_argument("--save-html", default=None, metavar="DIR",
                   help="把抓到的原始網頁另存到這個資料夾（新北市、台南市），用來檢查解析是否正確")


def _add_rerun(p: argparse.ArgumentParser, not_found: bool = False) -> None:
    p.add_argument("--refresh", action="store_true", help="已經查過的也重新查")
    if not_found:
        p.add_argument("--retry-not-found", action="store_true", help="重查之前「查無資料」的")


def _add_license_type(p: argparse.ArgumentParser) -> None:
    p.add_argument("--license-type", default="使用執照", metavar="TYPE",
                   help="執照類別（預設「使用執照」，也可以是建造執照／雜項執照／拆除執照）")


def _build_parser() -> argparse.ArgumentParser:
    search_cities = "、".join(cities_supporting(QueryType.YEAR))
    all_cities = "、".join(registry)
    parser = _new_parser(
        prog=PROG,
        description=(
            "多縣市使用執照查詢。查詢結果累積存進 SQLite 資料庫，查完自動匯出 CSV 到 data/output。\n"
            "已經查過的會自動跳過；中途中斷的話，重新執行同一個指令就會從中斷處繼續。\n\n"
            f"支援縣市：{all_cities}\n"
            f"整年度／門牌／地號查詢只支援：{search_cities}"
        ),
        epilog=f"每個指令的詳細說明：{PROG} <指令> --help\n例如：{PROG} batch --help",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<指令>")

    p = _add_command(
        sub, "batch", "整批查詢：依 CSV/Excel 清單逐筆查詢（所有縣市）",
        "輸入檔欄位：COMMUNITY_NO, CASE_NAME, CITY, DIST, ADDR_NO, LICENSE_NO, USE_FOR, 完整字號（選填）\n"
        "（舊格式：社區編號, 縣市, 行政區, 代表號地址, 使用執照年, 使用執照號 也支援）\n"
        "比對順序：完整字號 → 使用執照字號（年＋字＋號）→ 地址＋年號核對 → 年號＋行政區。\n"
        "對到多張或地址與年號矛盾時列為待確認（pending.csv），不存入；\n"
        "確認後把字號填進「完整字號」欄（都不對填「無」），再跑一次即可存入。\n"
        "成功、查無資料、待確認的下次會跳過；連線失敗的下次會自動重試。",
        ["batch --input data/input/community_from_images.csv",
         "batch --input data/input/sample.csv --retry-not-found"],
    )
    p.add_argument("--input", required=True, metavar="FILE", help="批次輸入檔，CSV 或 Excel（必填）")
    _add_rerun(p, not_found=True)
    p.add_argument("--retry-pending", action="store_true",
                   help="待確認的列也重新比對（程式的比對規則改進之後用）")
    _add_save_html(p)
    _add_output(p)
    _add_db(p)

    p = _add_command(
        sub, "license", "使用執照號：查單一張執照（所有縣市）",
        "同一個年＋號對到多張執照時會列為待確認（pending.csv），可用 --key 指定完整字號。",
        ["license --city 新北市 --year 115 --number 5",
         'license --city 桃園市 --year 75 --number 1 --key "(75)桃縣建管使其字第00001號"'],
    )
    p.add_argument("--city", required=True, metavar="CITY", help=f"（必填）{all_cities}")
    p.add_argument("--year", required=True, metavar="YEAR", help="民國年度，例如 115（必填）")
    p.add_argument("--number", required=True, metavar="NO", help="使用執照號，例如 1（必填）")
    p.add_argument("--key", default="", metavar="KEY",
                   help="完整字號，例如 (75)桃縣建管使其字第00001號；同年號有多張執照時用來指定")
    _add_rerun(p, not_found=True)
    _add_save_html(p)
    _add_output(p)
    _add_db(p)

    p = _add_command(
        sub, "year", f"整年度：查某縣市某一年的所有執照（{search_cities}）",
        "依發照日期篩選、自動翻頁。跑過的年度會跳過，但今年的資料還會增加，每次都會照常查詢。",
        ["year --city 桃園市 --year 114",
         "year --city 台中市 --year 114 --license-type 建造執照"],
    )
    p.add_argument("--city", required=True, metavar="CITY", help="（必填）")
    p.add_argument("--year", required=True, metavar="YEAR", help="民國年度，例如 115（必填）")
    _add_license_type(p)
    _add_rerun(p)
    _add_output(p)
    _add_db(p)

    p = _add_command(
        sub, "address", f"門牌：依門牌查執照（{search_cities}）",
        "各欄位可省略，但至少要給一項。純數字的門牌號會精準比對（7 不會比對到 17）。",
        ["address --city 桃園市 --district 八德區 --road 永豐路519巷2弄 --number 7"],
    )
    p.add_argument("--city", required=True, metavar="CITY", help="（必填）")
    p.add_argument("--district", default="", metavar="DISTRICT", help="例如 八德區")
    p.add_argument("--road", default="", metavar="ROAD", help="路街段巷弄，例如 永豐路519巷2弄")
    p.add_argument("--number", default="", metavar="NO", help="門牌號，例如 7")
    _add_license_type(p)
    _add_rerun(p)
    _add_output(p)
    _add_db(p)

    p = _add_command(
        sub, "land", f"地號：依地段地號查執照（{search_cities}）",
        "各欄位可省略，但至少要給一項。地號不用補零（990 會比對到 0990）。",
        ["land --city 桃園市 --section 高明段 --main-no 990",
         "land --city 台中市 --section 樂業段"],
    )
    p.add_argument("--city", required=True, metavar="CITY", help="（必填）")
    p.add_argument("--district", default="", metavar="DISTRICT", help="例如 八德區")
    p.add_argument("--section", default="", metavar="SECTION", help="例如 高明段")
    p.add_argument("--main-no", default="", metavar="NO", help="地號母號，例如 990")
    p.add_argument("--sub-no", default="", metavar="NO", help="地號子號，例如 0")
    _add_license_type(p)
    _add_rerun(p)
    _add_output(p)
    _add_db(p)

    p = _add_command(
        sub, "export", "匯出：把資料庫內容匯出成 CSV",
        "產生 license_main / license_address / license_land / errors 四個檔案（樓層、停車明細只存在資料庫）。",
        ["export", "export --output data/output/taoyuan --city 桃園市"],
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT, metavar="DIR", help=f"輸出目錄（預設 {DEFAULT_OUTPUT}）")
    p.add_argument("--city", default=None, metavar="CITY", help="只匯出某個縣市（預設全部）")
    _add_db(p)

    p = _add_command(
        sub, "check", "檢查：跑批次前先確認輸入檔有沒有被正確理解（不查詢、不動資料庫）",
        "輸出到 data/input/normalized/：\n"
        "  input_normalized.csv  正規化後的輸入檔，可以直接拿來跑 batch\n"
        "  input_check.csv       每一列解析出的年／字／號、地址拆解結果，並標出問題\n"
        "                        （縣市不支援、字號無法解析、地址沒有門牌號、重複列…）",
        ["check --input data/input/community_from_images.csv"],
    )
    p.add_argument("--input", required=True, metavar="FILE", help="批次輸入檔，CSV 或 Excel（必填）")
    p.add_argument("--output", default=DEFAULT_CHECK_OUTPUT, metavar="DIR",
                   help=f"輸出目錄（預設 {DEFAULT_CHECK_OUTPUT}）")

    p = _add_command(
        sub, "probe-tainan", "（開發用）台南市清單查詢探查：存下清單頁與明細頁，用來改進台南的比對",
        "輸入一次驗證碼後，用查詢頁的「執照號碼」清單查詢去查指定的年＋號（預設：資料庫裡台南市\n"
        "待確認的列），把清單頁和清單中每張執照的明細頁存到 --output。不會寫入資料庫。",
        ["probe-tainan", "probe-tainan --target 82 1136 --target 88 851"],
    )
    p.add_argument("--target", nargs=2, action="append", metavar=("YEAR", "NO"), help="要查的年、號，可重複指定")
    p.add_argument("--output", default="data/debug", metavar="DIR", help="存網頁的資料夾（預設 data/debug）")
    _add_db(p)

    p = _add_command(
        sub, "status", "進度：顯示各縣市累積筆數、查無資料與待重試數量", "",
        ["status"],
    )
    _add_db(p)

    return parser


def _dispatch(store: LicenseStore, args: argparse.Namespace) -> None:
    match args.command:
        case "batch":
            run_batch(store, args.input, args.refresh, args.retry_not_found, args.retry_pending)
        case "license":
            run_license(store, args.city, args.year, args.number, args.key, args.refresh, args.retry_not_found)
        case "year":
            params = {"year": args.year, "license_type": args.license_type}
            _run_search(
                store, args.city, QueryType.YEAR, params, args.refresh,
                lambda a: a.fetch_year(args.year, args.license_type),
            )
        case "address":
            q = AddressQuery(district=args.district, road=args.road, number=args.number)
            params = {"district": q.district, "road": q.road, "number": q.number, "license_type": args.license_type}
            _run_search(
                store, args.city, QueryType.ADDRESS, params, args.refresh,
                lambda a: a.search_by_address(q, args.license_type),
            )
        case "land":
            q = LandQuery(district=args.district, section=args.section, main_no=args.main_no, sub_no=args.sub_no)
            params = {
                "district": q.district, "section": q.section, "main_no": q.main_no, "sub_no": q.sub_no,
                "license_type": args.license_type,
            }
            _run_search(
                store, args.city, QueryType.LAND, params, args.refresh,
                lambda a: a.search_by_land(q, args.license_type),
            )
        case "export":
            run_export(store, args.output, args.city)
        case "status":
            run_status(store)
        case "probe-tainan":
            targets = [(y, n, "") for y, n in args.target] if args.target else pending_targets(store)
            if not targets:
                raise SystemExit("沒有要查的目標（資料庫裡台南市沒有待確認的列，可用 --target 年 號 指定）")
            run_probe(targets, args.output)


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "check":  # 不需要資料庫
        run_check(args.input, args.output)
        return
    if getattr(args, "save_html", None):
        CityAdapter.save_html_dir = Path(args.save_html)
    store = LicenseStore(args.db)
    auto_export = args.command in QUERY_COMMANDS and not args.no_export
    try:
        _dispatch(store, args)
    except KeyboardInterrupt:
        logger.warning("已中斷。已查完的都已存進資料庫，重新執行同一個指令就會從中斷處繼續。")
        if auto_export:
            run_export(store, args.output, None)
        sys.exit(130)
    else:
        if auto_export:
            run_export(store, args.output, None)
        elif args.command in QUERY_COMMANDS:
            logger.info(f"已存進資料庫 {store.path}，要看 CSV 請執行 export 指令")
    finally:
        store.close()


if __name__ == "__main__":
    main()
