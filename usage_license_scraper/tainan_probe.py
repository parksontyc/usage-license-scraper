"""台南市清單查詢的探查工具（開發用）。

目前台南用 IndexKey 直接開明細，IndexKey 裡沒有「字」，台南縣時期的執照找不到或會對到台南市
同年號的另一張。這個工具在人工輸入一次驗證碼後，用查詢頁的「執照號碼」清單查詢去查指定的年＋號，
把清單頁與清單裡每一張執照的明細頁存下來，用來決定怎麼把清單查詢接進正式流程。
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from loguru import logger

from usage_license_scraper.adapters.base import CityAdapter
from usage_license_scraper.adapters.tainan.adapter import TainanAdapter, extract_index_keys
from usage_license_scraper.adapters.tainan.parser import _cell
from usage_license_scraper.storage import STATUS_PENDING, LicenseStore


def pending_targets(store: LicenseStore) -> list[tuple[str, str, str]]:
    """資料庫裡台南市待確認的列：(年, 號, 輸入的字號)。"""
    rows = store.query(
        "SELECT license_year, license_number, license_text FROM license_tasks WHERE city = '台南市' AND status = ?",
        (STATUS_PENDING,),
    )
    return [(r["license_year"], r["license_number"], r["license_text"]) for r in rows]


def run_probe(targets: list[tuple[str, str, str]], out_dir: str) -> None:
    CityAdapter.save_html_dir = Path(out_dir)
    adapter = TainanAdapter()
    try:
        adapter.open_session()
    except RuntimeError as e:
        raise SystemExit(f"[台南市] {e}") from e
    folder = Path(out_dir) / adapter.city_name

    for i, (year, number, text) in enumerate(targets, start=1):
        logger.info(f"[{i}/{len(targets)}] 清單查詢 {year} 年第 {number} 號（輸入：{text}）")
        try:
            html = adapter.list_html(year, number)
        except Exception as e:  # noqa: BLE001 - 探查用，單筆失敗繼續下一筆
            logger.error(f"  清單查詢失敗：{e}")
            continue
        keys = extract_index_keys(html)
        text_only = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        logger.info(f"  清單頁 {len(html)} 字元，找到 IndexKey {len(keys)} 個；頁面開頭：{text_only[:80]}")
        for key in keys:
            try:
                detail = adapter.detail_html(key)
            except Exception as e:  # noqa: BLE001
                logger.error(f"    {key} 明細失敗：{e}")
                continue
            soup = BeautifulSoup(detail, "html.parser")
            logger.info(f"    {key} → {_cell(soup, '執照字號') or '（明細頁沒有執照字號）'}　{_cell(soup, '地　　址')[:30]}")

    logger.info(f"清單頁、明細頁都存在 {folder}（list_年_號.html、IndexKey.html），跑完請告訴我")
