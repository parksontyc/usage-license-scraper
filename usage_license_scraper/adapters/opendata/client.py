"""「全國建管系統 opendata」標準端點的 HTTP 查詢層。

    GET {host}/opendata/OpenDataSearchUrl.do?d=OPENDATA&c=BUILDLIC&Start=1&欄位=查詢條件...

完全公開、不需要登入、不需要驗證碼，單次最多回傳 100 筆（用 Start 翻頁）。
欄位說明見 `{host}/opendata/docs/a1.html`。

查詢條件的比對方式是「包含」＋正規式風格（支援 `^` `$` `.*`），所以
`門牌.號=7` 也會比對到 17 號、27 號；要精準比對時要自己加錨點，見
`exact_house_number()`／`exact_land_no()`。另外實測 `(` `)` 會被當成
字面字元而不是分組語法，所以 pattern 裡不能用括號做分組。

雙層欄位（樓層概要／地號／門牌）的查詢欄位名稱寫成「上層.下層」，例如
`門牌.路街段巷弄`、`地號.地號母號`（注意：文件寫「地號(母號)」，但實測
要用回傳 JSON 裡的欄位名稱「地號母號」才查得到）。

部分縣市的回應結尾會多一個雜訊控制位元組，直接 json.loads 會噴
trailing-data 錯誤，所以用 `clean_json_parse` 先把最後一個 `}` 之後的
內容丟掉再解析。
"""

from __future__ import annotations

import json

import requests
import urllib3

from usage_license_scraper.models import AddressQuery, LandQuery, is_misc_license

urllib3.disable_warnings()

PAGE_SIZE = 100


def clean_json_parse(text: str) -> dict:
    end = text.rfind("}")
    return json.loads(text[: end + 1])


def search(
    base_url: str,
    conditions: dict[str, str],
    session: requests.Session | None = None,
    max_pages: int | None = None,
) -> list[dict]:
    """用任意欄位條件查詢，自動翻頁回傳所有結果。

    max_pages=1 代表只要第一頁（用在只需要一筆的精準查詢）。
    部分縣市（已知高雄市）同一張執照會重複回傳，這裡依「核發執照字號」去重。
    """
    http = session or requests
    results: list[dict] = []
    seen: set[str] = set()
    start = 1
    page = 0
    while True:
        params = {"d": "OPENDATA", "c": "BUILDLIC", "Start": str(start), **conditions}
        resp = http.get(f"{base_url}/opendata/OpenDataSearchUrl.do", params=params, timeout=30, verify=False)
        resp.raise_for_status()
        data = clean_json_parse(resp.text).get("data") or []
        for item in data:
            key = item.get("核發執照字號", "")
            if key and key in seen:
                continue
            seen.add(key)
            results.append(item)
        page += 1
        if len(data) < PAGE_SIZE or (max_pages is not None and page >= max_pages):
            return results
        start += PAGE_SIZE


# ── 各種查詢方式的條件組裝 ──


def license_number_patterns(year: str, number: str) -> list[str]:
    """「核發執照字號」格式各縣市、各年代都不同：
    - 年度有括號 "(115)高市工建築使字第00001號"、沒括號 "115中都使字第00001號"
    - 升格前的舊年度，年份可能寫 (75) 也可能寫 (075)，流水號位數不固定
      （第0001號／第001號／第00001號），字別依核發的鄉鎮市公所而不同
    - 第與號之間可能夾分局代碼，例如 "第德00001號"

    查詢引擎把 `(` `)` 當字面字元，所以每種年份寫法分開查；流水號用
    `[^0-9]0*{n}號$` 不限位數。查回來的結果再由 adapter 用年與號精準核對。"""
    y, n = int(year), int(number)
    years = list(dict.fromkeys([f"{y:03d}", str(y)]))
    tail = f".*[^0-9]0*{n}號$"
    return [f"^({ys}){tail}" for ys in years] + [f"^{ys}{tail}" for ys in years]


def year_conditions(year: str, license_type: str) -> dict[str, str]:
    return {"執照類別": license_type, "發照日期": f"{year.zfill(3)}年"}


def exact_house_number(value: str) -> str:
    """純數字的門牌號轉成錨定 pattern，避免「7」比對到「17號」。
    含其他字元（例如「7之1號」）就原樣交給 API。"""
    v = value.strip().removesuffix("號")
    return f"^{v}號" if v.isdigit() else value.strip()


def exact_land_no(value: str) -> str:
    """地號母號／子號在資料裡是補零的 4 碼（例如「0990」），轉成
    `^0*990$` 讓使用者輸入「990」也能精準命中。"""
    v = value.strip()
    return f"^0*{int(v)}$" if v.isdigit() else v


def address_conditions(query: AddressQuery, license_type: str) -> dict[str, str]:
    cond = {"執照類別": license_type}
    if query.district:
        cond["門牌.行政區"] = query.district
    if query.road:
        cond["門牌.路街段巷弄"] = query.road
    if query.number:
        cond["門牌.號"] = exact_house_number(query.number)
    if len(cond) == 1:
        raise ValueError("門牌查詢至少要提供行政區、路街段巷弄、門牌號其中一項")
    return cond


def land_conditions(query: LandQuery, license_type: str) -> dict[str, str]:
    cond = {"執照類別": license_type}
    if query.district:
        cond["地號.行政區"] = query.district
    if query.section:
        cond["地號.地段"] = query.section
    if query.main_no:
        cond["地號.地號母號"] = exact_land_no(query.main_no)
    if query.sub_no:
        cond["地號.地號子號"] = exact_land_no(query.sub_no)
    if len(cond) == 1:
        raise ValueError("地號查詢至少要提供行政區、地段、地號其中一項")
    return cond


def exclude_misc(items: list[dict]) -> list[dict]:
    """「執照類別=使用執照」查詢會連雜項使用執照一起回傳，這裡排除掉。"""
    return [i for i in items if not is_misc_license(i.get("核發執照字號", ""), i.get("原領執照字號", ""))]


def search_by_number(
    base_url: str, year: str, number: str, license_type: str = "使用執照", session: requests.Session | None = None
) -> list[dict]:
    """年＋號的所有候選（各種字號寫法的聯集，依字號去重）。"""
    found: dict[str, dict] = {}
    for pattern in license_number_patterns(year, number):
        for item in search(base_url, {"執照類別": license_type, "核發執照字號": pattern}, session):
            found.setdefault(item.get("核發執照字號", ""), item)
    return list(found.values())


def search_by_key(
    base_url: str, key: str, license_type: str = "使用執照", session: requests.Session | None = None
) -> list[dict]:
    """完整字號精準查詢，例如 "(75)桃縣建管使其字第00001號"。"""
    return search(base_url, {"執照類別": license_type, "核發執照字號": f"^{key}$"}, session)


def address_candidate_conditions(query: AddressQuery, license_type: str = "使用執照") -> list[dict[str, str]]:
    """批次比對用的門牌查詢條件，比 address_conditions 寬鬆，結果再由 matcher 用完整地址精準核對。
    回傳多組條件，adapter 全部都查、結果合併：

    - 不帶行政區：各縣市「行政區」欄寫法不一（新竹市只寫「新竹市」、香山區放在村里鄰；
      舊資料是「桃園縣觀音鄉」），行政區交給 matcher 核對
    - 路名同時查「路街段巷弄」與「村里鄰」（新竹縣的「鹿寮坑」這類地名放在村里鄰）
    - 台／臺 寫法不一，用 `.` 代替；門牌號只錨定開頭（7 → ^7[^0-9]，涵蓋 7號、7-1號、7之1號）
    """
    road = query.road.replace("臺", ".").replace("台", ".")
    base = {"執照類別": license_type}
    if query.number:
        base["門牌.號"] = f"^{query.number.split('-')[0]}[^0-9]"
    return [{**base, "門牌.路街段巷弄": road}, {**base, "門牌.村里鄰": road}]
