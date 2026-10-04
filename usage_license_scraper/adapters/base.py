"""各縣市爬蟲的共用介面。

每個縣市查詢系統的表單欄位、驗證碼機制、回傳格式都不同，
所以每個縣市各自實作一個 CityAdapter 子類別；共用的批次流程
（讀輸入、分派、寫輸出）則統一走 cli.py。

各縣市能支援的查詢方式不一樣（例如新北市只能用執照號碼查，opendata 系統的
縣市還能查整年度／門牌／地號），所以每個 adapter 用 `supported_queries`
宣告自己支援哪些 QueryType，cli 依此判斷能不能執行；之後新竹縣、台南市要擴充
新的查詢方式時，只要實作對應方法並把 QueryType 加進 `supported_queries` 即可。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path

from usage_license_scraper.models import AddressQuery, InputRow, LandQuery, LicenseRecord

registry: dict[str, type["CityAdapter"]] = {}


def register(city_name: str):
    def _wrap(cls: type["CityAdapter"]):
        registry[city_name] = cls
        return cls

    return _wrap


class LicenseNotFound(ValueError):
    """查詢系統明確回應「查無此執照」。

    跟連線逾時等暫時性錯誤分開：查無資料的下次預設不再重查，
    暫時性錯誤則會在下次執行時自動重試。
    """


class QueryType(str, Enum):
    LICENSE_NUMBER = "使用執照號"  # 依年＋號查單筆（批次查詢也是逐筆走這個）
    YEAR = "整年度"
    ADDRESS = "門牌"
    LAND = "地號"


def cities_supporting(query: QueryType) -> list[str]:
    return [city for city, cls in registry.items() if query in cls.supported_queries]


class CityAdapter(ABC):
    """單一縣市查詢系統的爬蟲介面。"""

    city_name: str

    #: 資料來源系統，寫進資料庫的 source 欄位，用來判斷欄位空白是不是該系統本來就沒有。
    source: str = ""

    def record_source(self, record: LicenseRecord) -> str:
        """這張執照是從哪個系統查到的；一個縣市有多個來源時覆寫（例如新竹縣 opendata＋bupic）。"""
        return self.source

    #: 此縣市支援的查詢方式；預設只有用年＋號查單筆。
    supported_queries: frozenset[QueryType] = frozenset({QueryType.LICENSE_NUMBER})

    #: 此縣市查詢頁是否需要人工輸入驗證碼。
    #: True 時，cli 會先呼叫 open_session() 讓使用者手動過驗證碼一次，
    #: 之後同一次執行就重複使用該 session 查詢多筆資料。
    requires_captcha: bool = False

    def open_session(self) -> None:
        """建立／驗證 session。預設不需要，有驗證碼的縣市覆寫此方法。"""
        return None

    @abstractmethod
    def fetch(self, row: InputRow) -> LicenseRecord:
        """依據輸入列（年＋號為主要查詢鍵）查詢並回傳整合後的執照資料。

        查無資料時拋出 LicenseNotFound；其他失敗（連線、解析）拋出一般例外。
        由呼叫端（cli.py）統一記錄，而不是回傳一個欄位全空的 LicenseRecord。
        """
        raise NotImplementedError

    # ── 批次比對用的候選查詢（見 matcher.py）──

    def candidates_by_number(self, row: InputRow) -> list[LicenseRecord]:
        """年＋號對到的所有執照（年號不唯一時可能多張）。

        預設只有 fetch() 拿到的那一張；查詢系統能列出同年號所有執照的縣市
        （例如 opendata）應覆寫成回傳全部候選。
        """
        try:
            return [self.fetch(row)]
        except LicenseNotFound:
            return []

    def candidates_by_key(self, row: InputRow, key: str) -> list[LicenseRecord]:
        """完整字號對到的執照。預設用年＋號查，由 matcher 比對字號是否相同。"""
        return self.candidates_by_number(row)

    def candidates_by_address(self, address: AddressQuery) -> list[LicenseRecord]:
        """門牌對到的執照。只有支援 QueryType.ADDRESS 的縣市會被呼叫。"""
        return self.search_by_address(address)

    # ── 以下為選配的查詢方式，支援的縣市覆寫並加進 supported_queries ──

    def fetch_year(self, year: str, license_type: str = "使用執照") -> list[LicenseRecord]:
        """查某一整年、某種執照類別的所有資料。"""
        raise NotImplementedError(f"{self.city_name} 不支援整年度查詢")

    def search_by_address(self, query: AddressQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        """依門牌查詢，可能回傳多筆。"""
        raise NotImplementedError(f"{self.city_name} 不支援門牌查詢")

    def search_by_land(self, query: LandQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        """依地段地號查詢，可能回傳多筆。"""
        raise NotImplementedError(f"{self.city_name} 不支援地號查詢")

    def close(self) -> None:
        """釋放 session／瀏覽器等資源。預設不需要。"""
        return None

    #: 有設定的話，抓到的原始網頁另存到這個資料夾（cli 的 --save-html），用來檢查解析是否正確
    save_html_dir: Path | None = None

    def save_html(self, name: str, html: str) -> None:
        if self.save_html_dir is None:
            return
        folder = self.save_html_dir / self.city_name
        folder.mkdir(parents=True, exist_ok=True)
        # 只換掉檔名不能用的字元，中文保留（地址查詢的檔名要靠中文路名區分）
        safe = re.sub(r'[\\/:*?"<>|\s]', "_", name)
        (folder / f"{safe}.html").write_text(html, encoding="utf-8")
