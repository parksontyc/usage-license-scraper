"""批次查詢時，把一列輸入對應到唯一一張使用執照。

「年＋號」不是唯一的：升格前各鄉鎮市公所各自核發、各自從 1 號編起，例如桃園市
75 年第 1 號就有 9 張（中壢市公所、觀音鄉公所、桃園縣政府…），差在字號中間的
「字」。所以比對順序是：

1. 有填「完整字號」→ 只認這個字號（填「無」代表人工確認沒有對應執照）。
2. 輸入有使用執照字號（LICENSE_NO）→ 年、字、號都跟系統上某一張相同就採用。
   寫法差異（補零、有沒有「字第」）不影響，見 license_no.py。
3. 能用門牌查詢的縣市（opendata）→ 用地址查，再用年＋號核對，兩者都對上才採用；
   地址查到的執照年號都對不上 → 待確認。
4. 地址查不到（或沒填地址）→ 用年＋號查，再用行政區篩選。
5. 新北市／台南市只能用年＋號查 → 查到後核對門牌。
   3～5 剩多張時，再看「字」：輸入的字被候選的字包含（例如輸入「使」、系統「汐使」）
   而且只有一張符合，就採用。

不是唯一一張、或地址與年號互相矛盾時回傳 pending，候選全部列在 pending.csv，
不寫進執照資料表；人工確認後把字號填回輸入檔的「完整字號」欄，再跑一次就會存進去。

地址支援範圍與多個門牌：'河堤路302~312號、明賢街67~77號'、'圓通路369巷39之1~30號'、
'中正路105巷(全)'、'寶橋路235巷2、4號及235巷6弄2號'；樓層與結尾的「等」忽略。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from usage_license_scraper.adapters.base import CityAdapter, QueryType
from usage_license_scraper.district import district_from_address
from usage_license_scraper.license_no import LicenseNo, parse_license_no
from usage_license_scraper.models import AddressQuery, InputRow, LicenseRecord
from usage_license_scraper.normalize import _CN_NUM, normalize_address, normalize_license_text, normalize_text
from usage_license_scraper.storage import STATUS_NOT_FOUND, STATUS_OK, STATUS_PENDING

NO_MATCH_MARK = "無"  # 完整字號填這個 = 人工確認沒有對應的使用執照
MAX_ADDRESS_QUERIES = 6  # 一列地址最多拆出幾組去查（避免很長的地址清單打太多次 API）

_NUM = r"\d+(?:[之\-]\d+)?"


@dataclass
class MatchResult:
    status: str
    record: LicenseRecord | None = None
    candidates: list[LicenseRecord] = field(default_factory=list)
    matched_by: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AddressSpec:
    """輸入地址拆出來的一組比對條件；lo／hi 為 None 代表整條路巷（全）。
    parity：1＝只有單號、0＝只有雙號（'民治路41~45號(單號)'），None＝不限。"""

    district: str
    road: str
    lo: tuple[int, int] | None
    hi: tuple[int, int] | None
    parity: int | None = None

    def contains(self, number: tuple[int, int] | None) -> bool:
        if self.lo is None or self.hi is None:
            return True
        if number is None or not self.lo <= number <= self.hi:
            return False
        return self.parity is None or number[0] % 2 == self.parity

    def api_queries(self) -> list[AddressQuery]:
        """拿去查詢系統用的門牌條件。門牌號 '40之2' 寫成 '40-2'（opendata 只用主號，台南會拆成號／之號）。
        範圍地址除了只給路名，另外用起訖兩個門牌號各查一次：有些系統（台南）只給路名時
        清單只有前 10 筆，同一條路的執照多時會漏掉。"""
        def num(n: tuple[int, int]) -> str:
            return f"{n[0]}-{n[1]}" if n[1] else str(n[0])

        road_only = AddressQuery(district=self.district, road=self.road, number="")
        if self.lo is None or self.hi is None:
            return [road_only]
        if self.lo == self.hi:
            return [AddressQuery(district=self.district, road=self.road, number=num(self.lo))]
        return [
            AddressQuery(district=self.district, road=self.road, number=num(self.lo)),
            AddressQuery(district=self.district, road=self.road, number=num(self.hi)),
            road_only,
        ]


# ── 正規化 ──


def normalize_key(key: str) -> str:
    """完整字號比對用：全形轉半形、去空白、臺→台。"""
    return normalize_text(key)


def _district_stem(district: str) -> str:
    """'中壢區'／'中壢市' -> '中壢'，讓升格前後（鄉鎮市→區）的寫法也能對上。"""
    d = normalize_text(district)
    return d[:-1] if len(d) > 1 and d[-1] in "區鄉鎮市" else d


def _num(text: str) -> tuple[int, int]:
    """'13之6'／'13-6' -> (13, 6)；'7' -> (7, 0)"""
    parts = re.split(r"[之\-]", text)
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def _strip_prefix(text: str) -> str:
    """去掉開頭的縣市、行政區、村里鄰。"""
    m = re.match(r"^(?:\S{2}[市縣])?(?:\S{1,3}?[區鄉鎮市][區鄉鎮市]?(?=\S))?", text)
    rest = text[m.end():] if m else text
    # 村里＋鄰，例如「永豐里8鄰」「內厝里七鄰」；沒有鄰的「湖興里」也去掉，但不能把「萬里路」的「萬里」去掉
    rest = re.sub(rf"^\S{{1,4}}?[村里][\d{_CN_NUM}]+鄰", "", rest)
    rest = re.sub(r"^\S{1,3}?[村里](?![路街道巷弄段])(?=\S{2,})", "", rest)
    return rest


def parse_address(address: str) -> AddressQuery | None:
    """把單一門牌拆成「行政區／路街段巷弄／號」。樓層忽略。用來解析系統上執照的門牌。

    '桃園市八德區永豐里8鄰永豐路519巷2弄7號二樓' -> AddressQuery('八德區', '永豐路519巷2弄', '7')
    '新北市三芝區錫板里3鄰海尾13之6號'           -> AddressQuery('三芝區', '海尾', '13-6')
    """
    t = normalize_address(address)
    if not t:
        return None
    district = district_from_address(t)
    if len(district) == 3 and district[-1] in "市縣" and t.startswith(district):
        # 「臺南市勝利路50號」只寫到縣市、沒寫行政區：district_from_address 會把縣市當成行政區，
        # 這裡清掉，比對時就不檢查行政區（只比路名＋門牌號）
        district = ""
    rest = _strip_prefix(t)
    num = re.search(rf"({_NUM})號", rest)
    if not num:
        return AddressQuery(district=district, road=rest, number="")
    main, sub = _num(num.group(1))
    return AddressQuery(district=district, road=rest[: num.start()], number=f"{main}-{sub}" if sub else str(main))


def _road_base(road: str) -> str:
    """'寶橋路235巷' -> '寶橋路'；'中山東路三段47巷' -> '中山東路三段'"""
    m = re.match(rf"^.*?[路街道](?:[{_CN_NUM}\d]+段)?", road)
    return m.group(0) if m else road


def _resolve_road(road: str, prev: str) -> list[str]:
    """多個門牌的後面幾段常省略路名：'中正七街302~312號、308巷1~18號' 的第二段是
    中正七街308巷；'正德路339巷1號、341號' 的 341號 可能是正德路341號，也可能是
    339巷341號，兩種都列入比對。"""
    if not road:
        return [r for r in dict.fromkeys([prev, _road_base(prev)]) if r] if prev else []
    if prev and not re.search(r"[路街道]", road):
        return [_road_base(prev) + road]
    return [road]


def parse_address_specs(address: str, district: str = "") -> list[AddressSpec]:
    """把輸入的地址（可以是範圍、多個門牌、整條巷）拆成多組比對條件。"""
    t = normalize_address(address)
    t = re.sub(rf"(?:\d+|[{_CN_NUM}]+)樓(?:之\d+)?", "", t)
    if not t:
        return []
    district = district or district_from_address(t)
    t = _strip_prefix(t)

    specs: list[AddressSpec] = []
    prev = ""
    for seg in re.split(r"[、,，及]", t):
        if not seg:
            continue
        parity = 1 if re.search(r"\(?單號\)?$", seg) else (0 if re.search(r"\(?雙號\)?$", seg) else None)
        seg = re.sub(r"\(?[單雙]號\)?$", "", seg)
        whole = bool(re.search(r"\(全\)$|全$", seg))
        seg = re.sub(r"\(全\)$|全$", "", seg)
        m = None if whole else re.search(rf"({_NUM})號?(?:~({_NUM}))?號?$", seg)
        if m:
            lo = _num(m.group(1))
            hi = lo
            if m.group(2):
                hi = _num(m.group(2))
                if lo[1] and not re.search(r"[之\-]", m.group(2)):
                    hi = (lo[0], int(m.group(2)))  # '39之1~30' -> 39之1 ~ 39之30
            road = seg[: m.start()]
        else:
            lo = hi = None
            road = seg
        roads = _resolve_road(road, prev)
        specs.extend(AddressSpec(district, r, lo, hi, parity) for r in roads)
        if roads:
            prev = roads[0]
    return list(dict.fromkeys(s for s in specs if s.road))


def _input_specs(row: InputRow) -> list[AddressSpec]:
    return parse_address_specs(row.address, row.district) if row.address else []


# ── 單一條件比對 ──


def _record_districts(record: LicenseRecord) -> set[str]:
    """執照本身門牌、地號上的行政區。不看 record.district：新北市／台南市的 parser 會把輸入列的
    行政區填進去，拿來比對會變成永遠相符。"""
    ds = {a.district for a in record.addresses} | {l.district for l in record.lands}
    ds |= {district_from_address(a.full_address) for a in record.addresses}
    ds |= {district_from_address(l.section) for l in record.lands}
    return {_district_stem(d) for d in ds if d}


def district_matches(district: str, record: LicenseRecord) -> bool:
    return _district_stem(district) in _record_districts(record)


def _same_road(a: str, b: str) -> bool:
    """路名相同；只差結尾的「路／街／道」也算（輸入「港口270之1號」、系統「港口路270之1號」）。"""
    return a == b or (a and b and (a + "路" == b or b + "路" == a or a + "街" == b or b + "街" == a))


def address_matches(specs: list[AddressSpec], record: LicenseRecord) -> bool:
    """執照的任一門牌落在輸入地址的任一組條件內（路街相同、號在範圍內，行政區有的話也要相同）。"""
    for a in record.addresses:
        p = parse_address(a.full_address)
        if not p:
            continue
        number = _num(p.number) if p.number else None
        for s in specs:
            if not _same_road(p.road, s.road) or not s.contains(number):
                continue
            if s.district and p.district and _district_stem(p.district) != _district_stem(s.district):
                continue
            return True
    return False


def _word(record: LicenseRecord) -> str:
    parsed = parse_license_no(record.license_key)
    return parsed.word if parsed else ""


def _prefer_word(candidates: list[LicenseRecord], want: LicenseNo | None) -> list[LicenseRecord]:
    """輸入的「字」被候選的字包含（輸入「使」、系統「汐使」；輸入「桃縣工建使」、系統「桃縣工建使平」）。"""
    if not want or not want.word:
        return []
    return [c for c in candidates if want.word in _word(c)]


def _unique(records: list[LicenseRecord]) -> list[LicenseRecord]:
    seen: dict[str, LicenseRecord] = {}
    for r in records:
        seen.setdefault(normalize_key(r.license_key), r)
    return list(seen.values())


def _ok(row: InputRow, record: LicenseRecord, matched_by: str) -> MatchResult:
    record.community_id = row.community_id
    record.case_name = row.case_name
    record.use_for = row.use_for
    record.district = row.district or record.district
    return MatchResult(STATUS_OK, record=record, matched_by=matched_by)


def _pending(candidates: list[LicenseRecord], reason: str) -> MatchResult:
    return MatchResult(STATUS_PENDING, candidates=_unique(candidates), reason=reason)


def _narrow(row: InputRow, pool: list[LicenseRecord], want: LicenseNo | None, matched_by: str, reason: str) -> MatchResult:
    """pool 剩一張就採用；多張時用「字」再篩一次。"""
    if len(pool) == 1:
        return _ok(row, pool[0], matched_by)
    by_word = _prefer_word(pool, want)
    if len(by_word) == 1:
        return _ok(row, by_word[0], f"{matched_by}+字別")
    return _pending(pool, reason)


# ── 主流程 ──


def match_license(adapter: CityAdapter, row: InputRow) -> MatchResult:
    key = normalize_key(row.full_license_key)
    if key == NO_MATCH_MARK:
        return MatchResult(STATUS_NOT_FOUND, reason="人工確認：沒有對應的使用執照")
    if key:
        return _match_by_key(adapter, row, key)
    if not (row.license_year.isdigit() and row.license_number.isdigit()):
        return _match_without_year(adapter, row)

    by_number =_unique(adapter.candidates_by_number(row))
    misc = [c for c in by_number if c.is_misc]
    by_number = [c for c in by_number if not c.is_misc]

    want = parse_license_no(row.license_text) if row.license_text else None
    specs = _input_specs(row)
    if want and want.word:
        exact = [c for c in by_number if _word(c) == want.word]
        if len(exact) == 1:
            return _ok(row, exact[0], "使用執照字號" + _address_note(row, specs, exact[0]))
        if exact:
            by_number = exact

    address_searched = False
    if specs and QueryType.ADDRESS in adapter.supported_queries:
        address_searched = True
        by_address = _unique(
            [c for c in _search_address(adapter, specs) if not c.is_misc and address_matches(specs, c)]
        )
        number_keys = {normalize_key(c.license_key) for c in by_number}
        both = [c for c in by_address if normalize_key(c.license_key) in number_keys]
        if both:
            return _narrow(row, both, want, "門牌+年號", f"門牌與年號都相符的執照有 {len(both)} 張")
        if by_address:
            return _pending(by_address + by_number, "門牌查到的執照，年號與輸入不符")

    if not by_number:
        if misc:
            return MatchResult(STATUS_NOT_FOUND, reason=f"雜項使用執照，不收錄：{misc[0].license_key}")
        return MatchResult(STATUS_NOT_FOUND, reason="查無資料")
    return _decide_by_number(row, specs, by_number, address_searched, want)


def _match_without_year(adapter: CityAdapter, row: InputRow) -> MatchResult:
    """字號解析不出年份（例如「南工字472號」）：能用門牌查的縣市先用地址找候選，
    「號」與「字」都對上而且只有一張才採用，否則候選列入待確認讓人工判斷。"""
    hint = f"使用執照字號「{row.license_text}」解析不出年、號"
    specs = _input_specs(row)
    if not specs or QueryType.ADDRESS not in adapter.supported_queries:
        return MatchResult(STATUS_NOT_FOUND, reason=f"{hint}，請修正 LICENSE_NO 或在「完整字號」欄填入系統上的字號")
    by_address = _unique(
        [c for c in _search_address(adapter, specs) if not c.is_misc and address_matches(specs, c)]
    )
    if not by_address:
        return MatchResult(STATUS_NOT_FOUND, reason=f"{hint}，用地址也查無資料")
    text = normalize_license_text(row.license_text)
    m = re.search(r"(\d+)號?$", text)
    word = re.sub(r"[字第\d()]", "", text[: m.start()]) if m else ""
    hits = [
        c for c in by_address
        if m and (p := parse_license_no(c.license_key)) and p.number == str(int(m.group(1))) and (not word or word == p.word)
    ]
    if len(hits) == 1:
        return _ok(row, hits[0], "門牌+字號（輸入缺年份）")
    return _pending(by_address, f"{hint}，用地址查到 {len(by_address)} 張，請確認")


def _address_note(row: InputRow, specs: list[AddressSpec], record: LicenseRecord) -> str:
    """字號吻合時仍核對一下門牌，結果附註在比對方式後面，方便抽查。
    系統上的門牌是發照當時的，之後門牌整編、道路改名都會造成不同，所以只附註、不擋。"""
    if not specs or address_matches(specs, record):
        return "+門牌" if specs else ""
    if not record.addresses:
        return "（系統無門牌資料）"
    # 系統門牌只寫到縣市（「臺南市仁和路…」）或完全沒寫（「安北路168號」）時沒有行政區可比，算門牌不同
    known = _record_districts(record) - {_district_stem(record.city or row.city)}
    if row.district and known and _district_stem(row.district) not in known:
        return "（行政區與輸入不同）"
    return "（門牌與輸入不同）"


def _search_address(adapter: CityAdapter, specs: list[AddressSpec]) -> list[LicenseRecord]:
    queries = list(dict.fromkeys(q for s in specs for q in s.api_queries()))[:MAX_ADDRESS_QUERIES]
    found: list[LicenseRecord] = []
    for q in queries:
        found.extend(adapter.candidates_by_address(q))
    return found


def _match_by_key(adapter: CityAdapter, row: InputRow, key: str) -> MatchResult:
    hits = [c for c in adapter.candidates_by_key(row, key) if normalize_key(c.license_key) == key]
    if not hits:
        return MatchResult(STATUS_NOT_FOUND, reason=f"查無此完整字號：{row.full_license_key}")
    if len(hits) > 1:
        return _pending(hits, "完整字號對到多張執照")
    return _ok(row, hits[0], "完整字號")


def _decide_by_number(
    row: InputRow,
    specs: list[AddressSpec],
    candidates: list[LicenseRecord],
    address_searched: bool,
    want: LicenseNo | None,
) -> MatchResult:
    # 新北市／台南市：沒辦法用門牌查，就核對年號查到的執照門牌
    if specs and not address_searched:
        hits = [c for c in candidates if address_matches(specs, c)]
        if hits:
            return _narrow(row, hits, want, "年號+門牌", f"年號與門牌都相符的執照有 {len(hits)} 張")
        return _pending(candidates, "年號查到的執照，門牌與輸入不符")

    # opendata 門牌查無、或輸入沒填地址：用行政區篩
    note = "（門牌查無）" if address_searched else ""
    pool = candidates
    if row.district:
        pool = [c for c in candidates if district_matches(row.district, c)]
        if not pool:
            return _pending(candidates, f"年號查到的執照，行政區與輸入不符{note}")
    return _narrow(
        row, pool, want, ("年號+行政區" if row.district else "年號") + note, f"年號對到 {len(pool)} 張執照{note}"
    )
