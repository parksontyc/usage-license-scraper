# 使用執照批次查詢工具

輸入一份「社區編號、縣市、行政區、代表號地址、使用執照年、使用執照號」的批次清單，
依縣市分派到對應的查詢系統爬蟲，抓回使用執照的基本資料、建築規模資訊、營造資訊，
輸出成結構化的 CSV。

## 支援縣市

| 縣市 | 狀態 |
|------|------|
| 台南市 | ✅ 已實作 |
| 新北市 | ✅ 已實作 |
| 桃園市 | ✅ 已實作 |
| 新竹市 | ✅ 已實作 |
| 台中市 | ✅ 已實作 |
| 高雄市 | ✅ 已實作 |
| 新竹縣 | ✅ 已實作：民國 96 年以前用 opendata；之後的年度用 bupic 明細頁（需貼上瀏覽器查詢過的工作階段，見下方） |

六個縣市都不需要自動辨識/破解驗證碼：新竹市／台中市／高雄市／桃園市走的是
各縣市官方的「全國建管系統 opendata」公開 API（無需登入、無需驗證碼），
新北市是直接存取公開的明細頁 URL，只有台南市需要人工看圖輸入一次驗證碼
（之後同一次執行可以重複用 session 批次查）。

## 安裝

```bash
uv sync
```

## 輸入格式

CSV 或 Excel，表頭不分大小寫。**主要格式**（例如 `data/input/community_from_images.csv`）：

```
COMMUNITY_NO,CASE_NAME,CITY,DIST,ADDR_NO,LICENSE_NO,USE_FOR
B407000498,水湳大街,台中市,西屯區,中康街181號,85中工建使字第00350號,3
E807000263,某大樓,高雄市,三民區,河堤路302~312號、明賢街67~77號,(83)高市工建築使字第01299號,8
```

- **ADDR_NO**：不含縣市、行政區。可以是範圍或多個門牌：`302~312號`、`39之1~30號`、
  `A號、B號`、`235巷2、4號及235巷6弄2號`、`105巷(全)`；樓層和結尾的「等」會忽略。
- **LICENSE_NO**：使用執照字號，寫法不必統一（`83重使1639號`、`(84)工使字第459號`、
  `88年桃縣工建使字第壢1653號`、`97桃縣工建使字第蘆2077號等` 都可以），
  程式會拆出年、字、號來比對（見 `usage_license_scraper/license_no.py`）。
- **CASE_NAME**（案名）、**USE_FOR**（建物型態代碼）：原樣記錄，不影響查詢，輸出在 `community_license.csv`。
- **CITY**：寫法會先正規化，`臺中市`→`台中市`、舊縣名 `桃園縣`→`桃園市`、`台北縣`→`新北市`、
  `台中縣`／`台南縣`／`高雄縣` 同理；只寫 `台中` 會補成 `台中市`（`新竹`、`嘉義` 有市也有縣，不猜）。
  輸出時仍保留原本的寫法。
- 可以在最後加一欄選填的 **完整字號**（或 `FULL_LICENSE_KEY`），用途見下方「待確認怎麼處理」。

**跑批次前建議先執行 `check`**：不查詢、不動資料庫，輸出兩個檔案到 `data/input/normalized/`：

- `input_normalized.csv`：正規化後的輸入檔，欄位同原檔，另附解析出的「使用執照年」「使用執照號」，
  可以直接拿來跑 `batch`。用原始檔或正規化檔跑，都視為同一批資料（不會重複查、不會重複紀錄）。
- `input_check.csv`：每一列正規化後的縣市、解析出的年／字／號、地址拆解結果，並標出問題：

- 錯誤（跑批次一定失敗，建議先修）：縣市不支援、使用執照字號解析不出年和號
- 警告（可以照跑，結果可能要人工確認）：舊縣名轉換、沒有地址、地址有一段沒有門牌號、
  重複列、年份晚於今年、新竹縣民國 96 年以後（需要 bupic 工作階段）、缺少社區編號

舊格式（中文表頭，年、號分兩欄）也仍然支援：

```
社區編號, 縣市, 行政區, 代表號地址, 使用執照年, 使用執照號, 完整字號
TEST0001, 新北市, 林口區, 新北市林口區林口里1鄰仁愛路一段39巷66號, 115, 1,
```

範例檔：

- `data/input/community_from_images.csv`：主要格式，59 筆真實社區資料。實測結果：
  53 筆成功（49 筆字號吻合，其中 41 筆門牌也相符）、5 筆查無資料（4 筆新竹縣
  民國 95 年以後的資料不在系統上、1 筆新北市淡水）、1 筆待確認。
- `data/input/sample.csv`：舊格式，18 筆。14 筆是實測查得到的真實資料，涵蓋新北市、
  桃園市、新竹市、台中市、高雄市、新竹縣，其中包含新北市 541 戶的大社區
  （115芝使00005），以及桃園市 75 年第 1 號（同年號有 9 張）用地址分辨出正確那張
  （TEST0014）。TEST0015 是同一個年號但沒有地址，預期列入待確認。
  另外 3 筆是故意設計會失敗的（`TEST9xxx`：執照號不存在、新竹縣資料過舊、
  不支援的縣市），用來確認錯誤會正確寫進 `errors.csv`。
- `data/input/sample_tainan.csv`：台南市要人工輸入驗證碼，所以獨立成一份，
  避免整批測試時卡住等人輸入。

## 執行

每種查詢方式是一個子指令。查詢結果**累積**存進 SQLite 資料庫
`data/licenses.db`（可用 `--db` 指定其他檔案），每次查完會**自動把整個資料庫匯出成 CSV**
到 `data/output/`（可用 `--output` 指定目錄，加 `--no-export` 則只寫資料庫不匯出）：

| 子指令 | 查詢方式 | 支援縣市 |
|--------|----------|----------|
| `batch` | 整批查詢（CSV/Excel） | 全部 |
| `license` | 使用執照年＋號查單筆 | 全部 |
| `year` | 整年度 | 桃園市／新竹市／台中市／高雄市（新竹縣資料過舊） |
| `address` | 門牌 | 同上，另外台南市也支援 |
| `land` | 地號 | 同上 |
| `check` | 跑批次前檢查輸入檔（不查詢、不動資料庫） | — |

```bash
# 跑批次前先檢查輸入檔：輸出到 data/input/normalized/（input_check.csv、input_normalized.csv）
uv run python -m usage_license_scraper check --input data/input/community_from_images.csv

# 整批查詢
uv run python -m usage_license_scraper batch --input data/input/sample.csv

# 使用執照號
uv run python -m usage_license_scraper license --city 新北市 --year 115 --number 1

# 整年度（自動翻頁，依「發照日期」篩選）
uv run python -m usage_license_scraper year --city 桃園市 --year 115

# 門牌（欄位都可省略，但至少給一項；純數字門牌號會精準比對，7 不會比對到 17）
uv run python -m usage_license_scraper address --city 桃園市 --district 八德區 --road 永豐路519巷2弄 --number 7

# 地號（同上，至少給一項）
uv run python -m usage_license_scraper land --city 桃園市 --section 高明段 --main-no 990
```

```bash
# 手動從資料庫匯出 CSV（--city 可只匯出一個縣市）
uv run python -m usage_license_scraper export --output data/output

# 看各縣市累積了多少、有多少查無資料／失敗待重試
uv run python -m usage_license_scraper status
```

### 比對流程（batch／license）

「使用執照年＋號」**不是唯一的**：升格前各鄉鎮市公所各自核發、各自從 1 號編起，
例如桃園市 75 年第 1 號就有 9 張（中壢市公所、觀音鄉公所、桃園縣政府…），
差在字號中間的「字」。所以每一列輸入依序這樣比對：

1. 有填**完整字號** → 只認這個字號。
2. 有 **LICENSE_NO** → 年、字、號都跟系統上某一張相同就採用（補零、有沒有「字第」
   不影響）。仍會核對門牌，結果附註在 `MATCHED_BY`：「使用執照字號+門牌」或
   「使用執照字號（門牌與輸入不同）」。系統上的門牌是發照當時的，之後門牌整編、
   道路改名都會造成不同，所以只附註、不擋。
3. 能用門牌查詢的縣市（桃園市／新竹市／台中市／高雄市／新竹縣）→ 用**代表號地址**
   查，再用年＋號核對，兩者都對上才採用。
4. 地址查不到、或沒填地址 → 用**年＋號**查，再用**行政區**篩選，剩一張才採用。
5. 新北市只能用年＋號查 → 查到後核對門牌。台南市用查詢系統的「執照號碼」與「建築地址」清單查詢，
   流程同 opendata 縣市（台南縣時期的執照也查得到）。
   3～5 剩多張時，再看「字」：輸入的字被候選的字包含（輸入 `87使字`、系統 `87汐使字`）
   而且只有一張，就採用。

採用的方式記在 `license_main.csv` 的 `matched_by` 欄（完整字號／門牌+年號／
年號+門牌／年號+行政區…）。行政區比對會把升格前後的寫法視為相同（觀音鄉＝觀音區、
平鎮市＝平鎮區）。

以下情況**不會自動採用**，列入待確認（`pending.csv`），不存進執照資料：

- 年號對到多張，地址、行政區也分不出來
- 地址查到的執照，年號跟輸入不符（例如舊房子拆掉重建、或輸入檔打錯）
- 新北市／台南市查到的執照，門牌跟輸入不符

#### 待確認怎麼處理

1. 打開 `data/output/pending.csv`。每一列待確認的輸入，會把所有候選執照各列一行，
   附上候選的完整字號、發照日期、行政區、門牌、起造人、原領執照，以及待確認原因。
2. 判斷是哪一張後，把「候選完整字號」填回**輸入檔**同一列，兩種方式擇一：
   - 在輸入檔最後加一欄「完整字號」，貼在這欄（建議：原本的 LICENSE_NO 保持不動）。
     如果沒有一張是對的，填「無」。
   - 或直接把 LICENSE_NO 改成候選字號。
3. 重新執行同一個 `batch` 指令。有填完整字號的列會照字號精準查詢並存入
   （`matched_by` 記為「完整字號」）；填「無」的記為查無資料；其他已完成的列照樣跳過。
   處理完的列會從 `pending.csv` 消失。

單筆查詢也可以直接指定字號：

```bash
uv run python -m usage_license_scraper license --city 桃園市 --year 75 --number 1 --key "(75)桃縣建管使其字第00001號"
```

### 不重複跑、中斷續跑

- 每查完一筆就立刻寫進資料庫。中途中斷（Ctrl+C、斷網、關機）也不會遺失
  已完成的，**重新執行同一個指令就會從中斷處繼續**。
- 已查過的預設跳過：成功、查無資料、待確認的都不會再查（待確認的列填了
  完整字號後會重新處理）；連線逾時等暫時性錯誤會在下次執行時自動重試。
- 「查過了沒」以「縣市＋年＋號＋社區編號＋代表號地址」辨識：同一個年號、不同
  社區是不同的兩列，會各自比對。
- 同一張執照不管被批次、整年度、門牌、地號查到幾次，資料庫裡都只有一筆
  （以「縣市＋執照字號」為準，重查時更新為最新內容）。
- 整年度／門牌／地號查詢跑過也會跳過；但整年度查詢的年份若是**今年**，
  因為還會有新核發的執照，每次都會照常查詢。
- `--refresh`：已查過的也重新查。`--retry-not-found`：重查之前查無資料的。
  `--retry-pending`（batch）：待確認的列依目前的比對規則重新比對（程式改進比對規則後使用）。

`year`／`address`／`land` 可加 `--license-type` 指定其他執照類別（預設
「使用執照」，也可以是「建造執照」「雜項執照」「拆除執照」等）。對不支援
該查詢方式的縣市下指令，會列出有支援的縣市。

### 新竹縣（bupic 工作階段）

新竹縣的 opendata 只更新到民國 96 年左右，查不到的會改用縣府「建築執照存根查詢系統」（bupic）的
明細頁。明細頁要用已經在瀏覽器正常查詢過的工作階段才看得到，所以批次跑到需要 bupic 的列時會提示：

1. 用瀏覽器開 https://build.hsinchu.gov.tw/bupic/preLoginFormAction.do ，正常查詢任意一筆（輸入驗證碼）。
2. F12 → Application → Cookies → `build.hsinchu.gov.tw`，複製 `JSESSIONID` 的值，貼到終端機後按 Enter。

直接按 Enter 則這次不使用 bupic，這些列標成失敗、下次執行再查。工作階段中途失效時也一樣，
重新在瀏覽器查詢一筆、貼上新的 JSESSIONID 即可。程式只讀明細頁（每筆間隔 1.5 秒），
不使用 bupic 的查詢與驗證碼介面；`license_main.csv` 的 `source` 欄會標成 `hsinchu_bupic`。

若該縣市查詢系統需要圖形驗證碼（目前只有台南市），會先把驗證碼圖片存成
`tainan_captcha.jpg`（不會開瀏覽器），請開啟圖片後在終端機輸入一次驗證碼；
直接按 Enter 可以換一張。之後同一次執行會重複使用該 session 查詢台南市的所有
資料列；要查的台南市資料都已查過時，不會要求輸入驗證碼。其餘縣市都是公開
API／公開 URL，不需要驗證碼。

## 輸出

資料累積在 `data/licenses.db`（SQLite，可用 DB Browser for SQLite 等工具直接開），
所有縣市共用同一組資料表。每次查完（或執行 `export`）會匯出到 `data/output/`，
內容是資料庫裡**全部**累積的資料，不是只有這次查的：

| 檔案 | 內容 |
|------|------|
| `license_main.csv` | 每筆執照一列，含社區編號、案名、建物型態代碼（`use_for`）、`community_count`（對到幾個社區）、基本資料與建築規模欄位，以及 `source`（資料來源系統）、`matched_by`（比對方式）、`fetched_at`（查詢時間） |
| `license_address.csv` | 門牌明細（一執照對多列），含行政區 |
| `license_land.csv` | 地段地號明細，含行政區 |
| `community_license.csv` | 每一列批次輸入一列，欄位同輸入檔（COMMUNITY_NO、CASE_NAME…），後面接 `STATUS`（ok／not_found／pending／error）、`MATCHED_LICENSE_KEY`、`MATCHED_BY`、`NOTE`，以及重複提示（見下方） |
| `pending.csv` | 待確認：每列輸入的所有候選執照（中文表頭），處理方式見上方「待確認怎麼處理」 |
| `errors.csv` | 查無資料（`not_found`）或查詢失敗（`error`）的紀錄與原因，含社區編號、案名 |

`community_license.csv` 的重複提示（匯出時計算，不影響比對）：

| 欄位 | 意思 |
|------|------|
| `DUP_SEQ`、`DUP_COUNT` | 有幾個社區對到同一張執照、這是第幾個（依社區編號排序）。`DUP_COUNT > 1` 就是重複，可能是同一張使照涵蓋多期／多棟，也可能是某列 LICENSE_NO 有誤，建議人工看一下。只有比對成功的列有值 |
| `COMMUNITY_ROWS` | 同一個社區編號在輸入檔出現幾列（例如分期有多張使照）|

用兩個數字欄而不是「1/2」的寫法，是因為 CSV 用 Excel 開時「1/2」會被自動轉成日期。

明細表用 `city` + `license_key` 對應回 `license_main.csv`。樓層概要（`license_floor`）、
停車空間（`license_parking`）明細只存在資料庫，不匯出 CSV。

只收錄建築物的使用執照：雜項使用執照（招牌、水塔、圍牆等，字號或原領執照字號含「雜」）
會被排除，批次查詢遇到時記為查無資料，原因寫在 `errors.csv`。

## 架構

```
usage_license_scraper/
├── models.py          # InputRow / AddressQuery / LandQuery / LicenseRecord 等共用資料結構
├── input_loader.py    # 讀批次 CSV/Excel
├── storage.py         # SQLite 累積儲存、查詢狀態紀錄（跳過已查過的／續跑）
├── output_writer.py   # 從資料庫匯出 CSV
├── matcher.py         # 批次比對：完整字號 → 門牌+年號 → 年號+行政區，無法確定的列入待確認
├── district.py        # 從門牌推出行政區
├── cli.py             # 子指令：batch / license / year / address / land / export / status
└── adapters/
    ├── base.py            # CityAdapter 介面、QueryType、註冊表
    ├── opendata/          # ✅ 桃園市／新竹市／台中市／高雄市（全國建管系統 opendata API）
    │   ├── client.py      #    HTTP 查詢、翻頁、各查詢方式的條件組裝
    │   ├── parser.py      #    JSON → LicenseRecord
    │   ├── adapter.py     #    共用的 OpenDataLicenseAdapter
    │   └── cities.py      #    各縣市 base_url
    ├── new_taipei/        # ✅ 新北市（公開明細頁，只能用使用執照號查）
    │   ├── adapter.py
    │   └── parser.py
    ├── hsinchu_county/    # ✅ 新竹縣（opendata ＋ bupic 明細頁）
    │   ├── adapter.py
    │   └── parser.py
    │   └── adapter.py
    └── tainan/            # ✅ 台南市（NBUPIC，需人工驗證碼，待擴充）
        ├── adapter.py
        └── parser.py
```

每組 adapter 用 `supported_queries` 宣告自己支援哪些查詢方式（`QueryType`：
使用執照號／整年度／門牌／地號），cli 依此決定能不能執行。`CityAdapter`
已經定義好 `fetch()`（必要）以及 `fetch_year()`、`search_by_address()`、
`search_by_land()`（選配）這幾個方法。

擴充新竹縣／台南市的查詢方式時：
1. 在該縣市資料夾的 `adapter.py` 覆寫對應的方法（例如 `search_by_address()`）。
2. 把對應的 `QueryType` 加進 `supported_queries`。
3. 解析邏輯放在同資料夾的 `parser.py`。

新增一個縣市時：
1. 先檢查有沒有 `{該縣市建管系統 host}/opendata/docs/a1.html`，有的話在
   `adapters/opendata/cities.py` 加一個繼承 `OpenDataLicenseAdapter` 的類別、
   指定 `base_url` 就好，四種查詢方式自動都支援。
2. 沒有的話，在 `adapters/` 新增一個資料夾（比照 `new_taipei/`），繼承
   `CityAdapter`，用 `@register("縣市名")` 註冊，實作 `fetch()`，並在
   `adapters/__init__.py` 加入 import。
3. 把查詢結果對應填進 `models.LicenseRecord`（欄位已涵蓋基本資料／建築規模／營造資訊）。

## 注意事項

- 僅會在站點要求圖形驗證碼時（目前只有台南市），下載驗證碼圖片讓「人工」看圖輸入
  一次；不會寫自動辨識或破解驗證碼的程式碼。
- 請依各縣市網站的使用條款合理控制查詢頻率，程式內建查詢間隔可依需要調整。
