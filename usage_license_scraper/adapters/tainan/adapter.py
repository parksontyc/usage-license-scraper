"""台南市使用執照查詢（cloudbm.nlma.gov.tw / NBUPIC 系統）。

目前只支援用使用執照號碼查詢；之後要擴充其他查詢方式（例如門牌、地號），
在 TainanAdapter 覆寫對應的 search_by_* 方法並加進 supported_queries。

查詢流程：
  1. 下載驗證碼圖片存成 tainan_captcha.jpg，使用者看圖在終端機輸入一次（不開瀏覽器）。
  2. 驗證碼通過後，每一列用查詢頁的「執照號碼」清單查詢（nbupic_lst.jsp）列出同年號的所有執照，
     再用清單裡的 IndexKey 打 licInfo.jsp 抓明細。

IndexKey 組成（共 19 碼）：organ(3) + 年(3) + 種類(2) + 號碼補零(7) + 尾碼(4)
  例：IF0 + 115 + 03 + 0000001 + 0000
尾碼不是固定的：台南縣時期的執照是另一個尾碼，例如 82 年第 1136 號有兩張，
  (82)南工字第1136號     IF0 082 03 0001136 0000（台南市）
  (82)南縣使字第01136號  IF0 082 03 0001136 0010（台南縣）
所以不能自己組 IndexKey，要從清單查詢拿。

清單查詢要帶查詢頁上的兩個隨機值（每次開頁面都不同）：表單隱藏欄位 frm_query_para_PRIMARYID，
以及 NBUPICpkey 標頭（頁面 script 裡的一個 32 碼變數），另加 fromajax=true，格式跟網頁送出的相同。
「執照號碼」與「建築地址」是兩個不同的查詢頁（QryType=1／3），各有自己的 PRIMARYID。

建築地址查詢的欄位（QryType=3）：
  Qry_BMBADD_CITY=67000（臺南市）、Qry_BMBADD_DIST8=行政區代碼（例如 67000320＝東區，從查詢頁的選單讀）
  Qry_addrad2=路街段（含「路」字，例如 東興路、大同路二段）、addrad3=巷、addrad4=弄、
  addrad5=號、addrad6=之號（40之2號 -> addrad5=40、addrad6=2）
清單每頁只顯示 10 筆；只給路名（範圍地址、整條巷）時只會拿到前 10 筆。

注意：清單查詢只在驗證碼通過之後才送（open_session），跟網頁上的使用方式一致。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

from usage_license_scraper.adapters.base import CityAdapter, LicenseNotFound, QueryType, register
from usage_license_scraper.adapters.tainan.parser import build_record
from usage_license_scraper.models import AddressQuery, InputRow, LicenseRecord

urllib3.disable_warnings()

BASE_URL = "https://cloudbm.nlma.gov.tw"
ORGAN = "IF0"          # 台南市在 NBUPIC 平台的機關代碼
LICENSE_KIND = "03"    # 使用執照種類代碼
TAIL = "0000"          # IndexKey 尾碼，目前固定；若有多棟案件可能要改 0001/0002…
QUERY_PAGE = f"{BASE_URL}/NBUPIC/?organ={ORGAN}"
ADDRESS_PAGE = f"{BASE_URL}/NBUPIC/index.jsp?organ={ORGAN}&QryType=3"
CITY_CODE = "67000"    # 臺南市（建築地址查詢的縣市代碼）
QUERY_TYPE_LICENSE = "1"
QUERY_TYPE_ADDRESS = "3"
PAGES = {QUERY_TYPE_LICENSE: QUERY_PAGE, QUERY_TYPE_ADDRESS: ADDRESS_PAGE}
CAPTCHA_PATH = Path("tainan_captcha.jpg")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def make_index_key(organ: str, yy: str, kind: str, no: int | str, tail: str = TAIL) -> str:
    return f"{organ}{yy}{kind}{str(int(no)).zfill(7)}{tail}"


def parse_page_tokens(html: str) -> dict[str, str]:
    """查詢頁上清單查詢要帶的值：SYSID（標頭名稱前綴）、pkey（標頭值）、PRIMARYID（表單隱藏欄位）。"""
    sysid = re.search(r'id="P_SYSID"[^>]*value="([^"]+)"', html)
    pkey = re.search(r'var\s+\w+\s*=\s*"([0-9a-f]{32})";\s*var\s+SYSID', html)
    primary_id = re.search(r'id="frm_query_para_PRIMARYID"[^>]*value="([^"]+)"', html)
    if not (sysid and pkey and primary_id):
        raise RuntimeError("查詢頁格式跟預期不同，找不到清單查詢需要的 SYSID／pkey／PRIMARYID")
    return {"sysid": sysid.group(1), "pkey": pkey.group(1), "primary_id": primary_id.group(1)}


def parse_district_codes(html: str) -> dict[str, str]:
    """建築地址查詢頁的行政區選單：{'東區': '67000320', ...}"""
    select = re.search(r"<select[^>]*id='Qry_BMBADD_DIST8'.*?</select>", html, re.S)
    if not select:
        return {}
    return {name.strip(): code for code, name in re.findall(r"<option value='(\d+)'[^>]*>([^<]+)</option>", select.group(0))}


def split_road(road: str) -> tuple[str, str, str]:
    """'海安路三段985巷2弄' -> ('海安路三段', '985', '2')；'東興路' -> ('東興路', '', '')"""
    m = re.match(r"^(.*?)(?:(\d+)巷)?(?:(\d+)弄)?$", road)
    base, lane, alley = m.groups() if m else (road, None, None)
    return base, lane or "", alley or ""


def extract_index_keys(html: str) -> list[str]:
    """從清單頁抓出所有 IndexKey（19 碼：機關3＋年3＋種類2＋號7＋尾碼4）。"""
    # 尾碼通常是 4 位數字，但很舊的資料是「00--」，例如 IF007203007791900--
    keys = re.findall(r"(?<![0-9A-Za-z])([A-Z][A-Z0-9]{2}\d{3}[0-9A-Z]{2}\d{7}[0-9\-]{4})(?![0-9\-])", html)
    return list(dict.fromkeys(keys))


@register("台南市")
class TainanAdapter(CityAdapter):
    city_name = "台南市"
    source = "nbupic"
    supported_queries = frozenset({QueryType.LICENSE_NUMBER, QueryType.ADDRESS})
    requires_captcha = True

    def __init__(self) -> None:
        self.session: requests.Session | None = None
        self.ua: str = ""
        self.page_tokens: dict[str, dict[str, str]] = {}  # 查詢方式 -> 該查詢頁的隨機值
        self.district_codes: dict[str, str] = {}

    def open_session(self) -> None:
        """下載驗證碼圖片存成檔案，使用者看圖在終端機輸入一次，通過後同一個 session 批次查詢。
        不開瀏覽器：驗證碼圖片本身就是一個網址（VaildImage.do），直接用 requests 下載。"""
        session = requests.Session()
        session.verify = False
        session.headers["User-Agent"] = USER_AGENT
        self._load_page(QUERY_TYPE_LICENSE, session)  # 先開查詢頁，拿到 session cookie 與清單查詢要帶的隨機值

        CAPTCHA_PATH.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 6):
            img = session.get(f"{BASE_URL}/NBUPIC/VaildImage.do", params={"t": int(time.time() * 1000)}, timeout=30)
            CAPTCHA_PATH.write_bytes(img.content)
            print(f"[台南市] 第 {attempt} 次：驗證碼圖片已存到 {CAPTCHA_PATH.resolve()}")
            code = input("  請開啟圖片，輸入驗證碼（直接按 Enter 換一張）：").strip()
            if not code:
                continue
            resp = session.post(
                f"{BASE_URL}/NBUPIC/VaildImageCheck.do",
                data={"validCodeStr": code},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Referer": QUERY_PAGE,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=30,
            )
            m = re.search(r"<ISOK>(.*?)</ISOK>", resp.text, re.IGNORECASE)
            if m and m.group(1).strip().lower() == "true":
                print("  驗證碼通過")
                self.session = session
                self.ua = USER_AGENT
                return
            print("  驗證碼錯誤，換一張重試")
        raise RuntimeError("驗證碼 5 次均失敗，無法建立 session")

    def detail_html(self, index_key: str) -> str:
        """licInfo.jsp 明細頁。IndexKey 的組成見本檔開頭。"""
        if self.session is None:
            raise RuntimeError("尚未呼叫 open_session() 建立查詢 session")
        yy, no = index_key[3:6], index_key[8:15]
        url = (
            f"{BASE_URL}/NBUPIC/licInfo.jsp"
            f"?IndexKey={index_key}"
            f"&license_no1={int(no)}"
            f"&license_kind={index_key[6:8]}"
            f"&license_yy={yy}"
            f"&organ={index_key[:3]}"
            f"&responseText=true"
        )
        resp = self.session.get(url, headers={"Referer": QUERY_PAGE}, timeout=30)
        resp.encoding = "utf-8"
        self.save_html(index_key, resp.text)
        return resp.text

    def _load_page(self, query_type: str, session: requests.Session | None = None) -> dict[str, str]:
        """開查詢頁，讀出清單查詢要帶的隨機值（建築地址頁另外讀行政區代碼）。"""
        page = (session or self.session).get(PAGES[query_type], timeout=30)
        page.encoding = "utf-8"
        self.page_tokens[query_type] = parse_page_tokens(page.text)
        if query_type == QUERY_TYPE_ADDRESS:
            self.district_codes = parse_district_codes(page.text)
        return self.page_tokens[query_type]

    def _list(self, query_type: str, fields: dict[str, str], name: str) -> str:
        """送出清單查詢（nbupic_lst.jsp），欄位與標頭跟網頁按「執行查詢」送出的相同。
        隨機值過期等原因失敗時，重開一次查詢頁再試。"""
        if self.session is None:
            raise RuntimeError("尚未呼叫 open_session() 建立查詢 session（需先通過驗證碼）")
        for attempt in (1, 2):
            t = self.page_tokens.get(query_type) or self._load_page(query_type)
            data = {
                "Qry_LICENSING_UNIT": ORGAN,
                "Qry_QryType": query_type,
                **fields,
                "Qry_imageCodetxt": "",
                "frm_query_para_PRIMARYID": t["primary_id"],
                "frm_query_para_sortKeys": "null",
                "fromajax": "true",
                "QueryParamButton_executeQuery": "執行查詢",
            }
            resp = self.session.post(
                f"{BASE_URL}/NBUPIC/nbupic_lst.jsp?queryparammode=true",
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    f"{t['sysid']}pkey": t["pkey"],
                    "Referer": PAGES[query_type],
                    "Origin": BASE_URL,
                },
                timeout=30,
            )
            resp.encoding = "utf-8"
            if resp.status_code == 200 and "目前無法使用" not in resp.text:
                self.save_html(name, resp.text)
                return resp.text
            self.page_tokens.pop(query_type, None)
        raise RuntimeError(f"清單查詢失敗（HTTP {resp.status_code}）：{name}")

    def list_html(self, year: str, number: str) -> str:
        """「執照號碼」清單查詢。"""
        fields = {
            "Qry_license_yy": year.zfill(3),
            "Qry_license_kind": LICENSE_KIND,
            "Qry_license_no1": str(int(number)).zfill(5),
        }
        return self._list(QUERY_TYPE_LICENSE, fields, f"list_{year.zfill(3)}_{int(number)}")

    def address_list_html(self, address: AddressQuery) -> str:
        """「建築地址」清單查詢。路名拆成路街段／巷／弄，門牌號 '40-2' 拆成號 40、之號 2。"""
        if not self.district_codes:
            self._load_page(QUERY_TYPE_ADDRESS)
        district = ""
        if address.district:
            stem = address.district.rstrip("區鄉鎮市")
            district = next((code for name, code in self.district_codes.items() if name.rstrip("區鄉鎮市") == stem), "")
        road, lane, alley = split_road(address.road)
        main, _, sub = address.number.partition("-")
        fields = {
            "Qry_license_yy": "",
            "Qry_BMBADD_CITY": CITY_CODE,
            "Qry_BMBADD_DIST8": district,
            "Qry_addrad1": "",
            "Qry_addrad2": road,
            "Qry_addrad3": lane,
            "Qry_addrad4": alley,
            "Qry_addrad5": main,
            "Qry_addrad6": sub,
            "Qry_addrad6_1": "",
            "Qry_addrad7": "",
            "Qry_addrad7_1": "",
            "Qry_addrad8": "",
        }
        name = f"addr_{address.district}_{address.road}_{address.number or '全'}"
        return self._list(QUERY_TYPE_ADDRESS, fields, name)

    def candidates_by_address(self, address: AddressQuery) -> list[LicenseRecord]:
        """建築地址清單查到的使照，逐一抓明細。年、號從 IndexKey 取（不是輸入列的年號）。"""
        candidates = []
        for index_key in extract_index_keys(self.address_list_html(address)):
            if index_key[6:8] != LICENSE_KIND:
                continue
            row = InputRow("", self.city_name, address.district, "", index_key[3:6], str(int(index_key[8:15])))
            record = self._record(row, index_key)
            if record is not None:
                candidates.append(record)
        return candidates

    def search_by_address(self, query: AddressQuery, license_type: str = "使用執照") -> list[LicenseRecord]:
        return [c for c in self.candidates_by_address(query) if not c.is_misc]

    def candidates_by_number(self, row: InputRow) -> list[LicenseRecord]:
        """清單查詢列出同年號的所有使照（台南市、台南縣時期都有），逐一抓明細。"""
        candidates = []
        for index_key in extract_index_keys(self.list_html(row.license_year, row.license_number)):
            if index_key[6:8] != LICENSE_KIND:
                continue
            record = self._record(row, index_key)
            if record is not None:
                candidates.append(record)
        return candidates

    def _record(self, row: InputRow, index_key: str) -> LicenseRecord | None:
        html = self.detail_html(index_key)
        if not html.strip() or "AjaxTableid" not in html:
            return None
        if "執照字號" not in html and "原領執照字號" not in html:
            return None
        return build_record(row, BeautifulSoup(html, "html.parser"))

    def fetch(self, row: InputRow) -> LicenseRecord:
        """年號剛好只對到一張時回傳；批次比對走 candidates_by_number（matcher.py）。"""
        candidates = self.candidates_by_number(row)
        if len(candidates) != 1:
            raise LicenseNotFound(f"{row.license_year} 年第 {row.license_number} 號對到 {len(candidates)} 張執照")
        return candidates[0]
