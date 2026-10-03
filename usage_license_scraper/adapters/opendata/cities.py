"""走 opendata 標準端點的縣市清單，各縣市只差在 base_url。

已實測（皆為 115 年第 00001 號使用執照，資料是最新的）：
- 桃園市：(115)桃市都施使字第德00001號。桃園市是最早驗證出這套做法的縣市，
  官方文件 https://building.tycg.gov.tw/opendata/docs/a1.html
- 新竹市：(115)府都使字第00001號
- 台中市：115中都使字第00001號（年度沒有括號）
- 高雄市：(115)高市工建築使字第00001號。跟原本走 bupic 搜尋頁（需要人工過
  驗證碼）查到的是同一筆資料，這支公開 API 可以完全取代掉驗證碼那條路。
  schema 少了「建築物用途」「土地使用分區」「起造人地址」欄位。
"""

from __future__ import annotations

from usage_license_scraper.adapters.base import register
from usage_license_scraper.adapters.opendata.adapter import OpenDataLicenseAdapter


@register("桃園市")
class TaoyuanAdapter(OpenDataLicenseAdapter):
    city_name = "桃園市"
    base_url = "https://building.tycg.gov.tw"


@register("新竹市")
class HsinchuCityAdapter(OpenDataLicenseAdapter):
    city_name = "新竹市"
    base_url = "https://build.hccg.gov.tw"


@register("台中市")
class TaichungAdapter(OpenDataLicenseAdapter):
    city_name = "台中市"
    base_url = "https://mcgbm.taichung.gov.tw"


@register("高雄市")
class KaohsiungAdapter(OpenDataLicenseAdapter):
    city_name = "高雄市"
    base_url = "https://buildmis.kcg.gov.tw"
