"""共用資料模型：輸入批次資料列，以及各縣市爬蟲回傳的執照資料。"""

from __future__ import annotations

from dataclasses import dataclass, field


def is_misc_license(license_key: str, original_license: str = "") -> bool:
    """是否為雜項使用執照（招牌、水塔、圍牆等雜項工作物）。本專案只收建築物的使用執照。

    各縣市標示方式不同：
    - 桃園市：字號含「雜」，例如 (113)桃市都施使字第雜溪00060號
    - 新竹市：字號含「雜」，例如 (113)府都雜使字第00013號
    - 高雄市：字號跟一般使照相同，但原領執照是雜項執照，例如
      (113)高市工建築使字第01500號 ← 原領 (113)高市工建築雜字第00016號
    """
    return "雜" in license_key or "雜" in original_license


@dataclass
class InputRow:
    """使用者批次上傳的一列查詢條件。"""

    community_id: str      # 社區編號
    city: str               # 縣市
    district: str           # 行政區
    address: str            # 代表號地址
    license_year: str       # 使用執照年
    license_number: str     # 使用執照號
    full_license_key: str = ""  # 完整字號（選填），例如 (75)桃縣建管使其字第00001號；填「無」代表人工確認沒有對應執照
    license_text: str = ""  # 輸入檔原始的使用執照字號（LICENSE_NO），例如 83重使1639號；年、號由此解析
    case_name: str = ""     # 案名（CASE_NAME）
    use_for: str = ""       # USE_FOR 建物型態代碼，原樣保留
    input_city: str = ""    # 輸入檔原始的縣市寫法（city 是正規化後的，例如 臺中市 -> 台中市）
    input_address: str = ""  # 輸入檔原始的地址寫法（address 是正規化後的）

    @property
    def key(self) -> str:
        return f"{self.city}-{self.license_year}-{self.license_number}"


@dataclass(frozen=True)
class AddressQuery:
    """門牌查詢條件。欄位留空代表不限制。"""

    district: str = ""   # 行政區，例如「八德區」
    road: str = ""       # 路街段巷弄，例如「永豐路519巷2弄」
    number: str = ""     # 門牌號，例如「7」或「7號」


@dataclass
class LandQuery:
    """地號查詢條件。欄位留空代表不限制。"""

    district: str = ""   # 行政區
    section: str = ""    # 地段，例如「高明段」
    main_no: str = ""    # 地號母號，例如「990」
    sub_no: str = ""     # 地號子號，例如「0」


@dataclass
class AddressRecord:
    license_key: str = ""
    seq: str = ""
    full_address: str = ""
    district: str = ""      # 行政區；parser 沒填的話，存檔時從 full_address 推出


@dataclass
class LandRecord:
    license_key: str = ""
    seq: str = ""
    section: str = ""       # 地段
    land_number: str = ""   # 地號
    area: str = ""
    district: str = ""      # 行政區；parser 沒填的話，存檔時從 section 推出


@dataclass
class FloorRecord:
    seq: str = ""
    building_no: str = ""   # 棟別
    floor: str = ""         # 層別
    floor_height: str = ""
    applied_area: str = ""
    balcony_area: str = ""
    terrace_area: str = ""
    use_group: str = ""     # 使用類組


@dataclass
class ParkingRecord:
    seq: str = ""
    category: str = ""
    legal_or_self: str = ""
    indoor_outdoor: str = ""
    count: str = ""
    area: str = ""


@dataclass
class LicenseRecord:
    """單一使用執照的整合資料（基本資料／建築規模／營造資訊）。"""

    @property
    def is_misc(self) -> bool:
        return is_misc_license(self.license_key, self.original_license)

    # 來源對應
    community_id: str = ""
    case_name: str = ""             # 案名（批次輸入的 CASE_NAME）
    use_for: str = ""               # 建物型態代碼（批次輸入的 USE_FOR）
    city: str = ""
    district: str = ""
    license_year: str = ""
    license_number: str = ""

    # ── 基本資料 ──
    license_key: str = ""           # 執照字號（原始格式）
    original_license: str = ""      # 原領執照字號
    issue_date: str = ""            # 發照日期
    builder: str = ""                # 起造人
    designer_name: str = ""          # 設計人姓名
    designer_firm: str = ""          # 設計人事務所
    supervisor_name: str = ""        # 監造人姓名
    supervisor_firm: str = ""        # 監造人事務所
    contractor_name: str = ""        # 承造人姓名
    contractor_firm: str = ""        # 承造人營造廠
    representative_address: str = "" # 代表地址
    land_number: str = ""            # 地號（主表欄位，單筆）

    # ── 建築規模資訊 ──
    use_zone: str = ""               # 使用分區
    site_area_arcade: str = ""       # 基地面積(騎樓)
    site_area_other: str = ""        # 基地面積(其他)
    site_area_setback: str = ""      # 基地面積(退縮地)
    site_area_total: str = ""        # 基地面積(合計)
    floor_building_household: str = ""  # 層棟戶數
    legal_open_space_area: str = ""  # 法定空地面積
    design_coverage_ratio: str = ""  # 設計建蔽率
    total_floor_area: str = ""       # 總樓地板面積
    design_far: str = ""             # 設計容積率
    building_height: str = ""        # 建物高度
    construction_type: str = ""      # 建造類別
    structure_type: str = ""         # 構造種類
    building_area: str = ""          # 建築面積
    building_area_other: str = ""    # 建築面積(其他)
    shelter_area_above: str = ""     # 防空避難面積(地上)
    shelter_area_below: str = ""     # 防空避難面積(地下)
    misc_engineering: str = ""       # 雜項工程
    construction_cost: str = ""      # 工程造價
    public_use_building: str = ""    # 公眾使用建築物

    floors: list[FloorRecord] = field(default_factory=list)
    parkings: list[ParkingRecord] = field(default_factory=list)
    addresses: list[AddressRecord] = field(default_factory=list)
    lands: list[LandRecord] = field(default_factory=list)

