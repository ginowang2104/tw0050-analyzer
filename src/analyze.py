"""
台股 0050 成份股異動分析系統 v6.3
─────────────────────────────────────────────────────────
修正項目：
  1. 0050 hardcode 更新為 2026-05-08 最新資料（50檔）
  2. 0050 成份股抓取：優先 TWSE 官方 ETF API → hardcode
     （移除 wantgoo/yuanta，解決 GitHub Actions 403/解析錯誤）
  3. 市值計算：改用 TWSE t187ap03_L 取得公司總發行股數，
     不再誤用 0050 ETF 持倉量（商品數量）計算市值
  4. 403 備援：收盤價加入本地快取，封鎖時自動讀取上次成功資料
  5. index.html 加入時間戳 + no-cache meta，解決 GitHub Pages 快取問題
  6. [v6.2] is_listed 排除 9xxx 台灣存託憑證（DR）
     t187ap03_L 欄位動態偵測 + 合理性驗證
     市值計算加入 Method B（PBratio）/ Method C（TradeValue）備援，
     避免 t187ap03_L 失敗時 exit code 1
  7. [v6.3] 異動分析加入預估買進／賣出張數
     考慮目前股價、0050規模（AUM = Σ股價×持倉股數）、預估成份股權重

市值計算優先順序：
  方法 A：ClosingPrice × t187ap03_L 總發行股數（正確市值）
  方法 B：ClosingPrice² ÷ BWIBBU_ALL.PBratio（代理估算）
  方法 C：TradeValue 排名（最後備援）
"""

import re
import json
import time
import requests
import urllib3
from datetime import datetime, date, timedelta
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ADD_THRESHOLD = 42
DEL_THRESHOLD = 57
TOP_N         = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.twse.com.tw/",
}

# ── 最新 0050 成份股（2026-05-08 資料，來源：元大投信）──────
# 格式：{代號: (名稱, 商品數量, 權重%)}
LATEST_0050: dict[str, tuple[str, int, float]] = {
    "2330": ("台積電",      475319215, 59.36),
    "2454": ("聯發科",       28621848,  6.21),
    "2308": ("台達電",       37788348,  4.74),
    "2317": ("鴻海",        237377355,  3.34),
    "3711": ("日月光投控",    64072338,  1.92),
    "2383": ("台光電",        5586374,  1.51),
    "2345": ("智邦",          9669646,  1.40),
    "3037": ("欣興",         25969830,  1.25),
    "2303": ("聯電",        226246596,  1.20),
    "2891": ("中信金",       344751787,  1.07),
    "2382": ("廣達",         51422602,  0.99),
    "2360": ("致茂",          7194327,  0.95),
    "3017": ("奇鋐",          6320731,  0.90),
    "2881": ("富邦金",       161979441,  0.86),
    "2882": ("國泰金",       181754945,  0.80),
    "2885": ("元大金",       211720010,  0.67),
    "2327": ("國巨",         30135871,  0.66),
    "6669": ("緯穎",          2009130,  0.60),
    "2887": ("台新新光金",   446522284,  0.59),
    "2412": ("中華電",        74079694,  0.56),
    "2884": ("玉山金",       297933985,  0.54),
    "2368": ("金像電",        6682152,  0.52),
    "2357": ("華碩",         13257238,  0.51),
    "2886": ("兆豐金",       229272813,  0.51),
    "1303": ("南亞",         95646379,  0.49),
    "2301": ("光寶科",        37706029,  0.47),
    "3231": ("緯創",         59383351,  0.46),
    "3661": ("世芯-KY",       1542505,  0.46),
    "2890": ("永豐金",       246223067,  0.42),
    "2344": ("華邦電",        60572540,  0.40),
    "1216": ("統一",         95585206,  0.39),
    "2883": ("凱基金",       306638191,  0.39),
    "7769": ("鴻勁",          1107620,  0.39),
    "2408": ("南亞科",        21801970,  0.37),
    "3653": ("健策",          1636231,  0.37),
    "2449": ("京元電子",      21112822,  0.35),
    "2892": ("第一金",       210641017,  0.35),
    "2059": ("川湖",          1102235,  0.34),
    "2880": ("華南金",       180592207,  0.34),
    "3008": ("大立光",        1863680,  0.27),
    "5880": ("合庫金",       203134190,  0.26),
    "2603": ("長榮",         21092712,  0.25),
    "2395": ("研華",          8913578,  0.24),
    "2002": ("中鋼",        216976515,  0.23),
    "1301": ("台塑",         81443177,  0.22),
    "4904": ("遠傳",         34432976,  0.19),
    "3045": ("台灣大",        27438850,  0.17),
    "2207": ("和泰車",        5432538,  0.14),
    "6919": ("康霈",         18693813,  0.11),
    "6505": ("台塑化",        23112964,  0.07),
}

FALLBACK_0050 = {k: v[0] for k, v in LATEST_0050.items()}

# 快取路徑（GitHub Actions 封鎖時，讀取上次成功的資料）
CACHE_PATH        = Path(__file__).parent.parent / "docs" / ".cache_prices.json"
CACHE_SHARES_PATH = Path(__file__).parent.parent / "docs" / ".cache_shares.json"
# 發行股數快取版本：版本不符時強制重新抓取
# v2 = 扣庫藏股；v3 = 加入全股/千股單位自動偵測
CACHE_SHARES_VERSION = "v3-unit-autofix"
# 成份股來源路徑（可直接用文字編輯器更新，免改 Python）
COMP_CSV_PATH = Path(__file__).parent.parent / "0050_component_paste.txt"


def http_get(url: str, timeout: int = 30) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
        if r.status_code in (403, 429):
            print(f"  [{r.status_code}] {url}")
            return None
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [WARN] {url}\n         → {e}")
        return None


def to_float(s) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return 0.0


def is_listed(code: str) -> bool:
    # 9xxx = 台灣存託憑證（DR），不列入上市普通股
    return bool(re.fullmatch(r"[1-8]\d{3}", str(code).strip()))


# ── 收盤價本地快取（應對 403 封鎖）─────────────────────────

def load_prices_cache() -> dict:
    """讀取上次成功抓取的收盤價快取（48 小時內有效）"""
    try:
        if CACHE_PATH.exists():
            obj = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            age_h = (time.time() - obj.get("_ts", 0)) / 3600
            if age_h < 48:
                print(f"  [快取] 使用 {age_h:.1f} 小時前的收盤價快取資料")
                return obj.get("prices", {})
            else:
                print(f"  [快取] 快取已過期（{age_h:.1f} 小時），略過")
    except Exception as e:
        print(f"  [快取] 讀取失敗：{e}")
    return {}


def save_prices_cache(prices: dict):
    """儲存收盤價快取（隨 index.html 一併 commit 即可跨次使用）"""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        obj = {"_ts": time.time(), "prices": prices}
        CACHE_PATH.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  [快取] 儲存失敗：{e}")


# ══════════════════════════════════════════════════════
# Step 1：收盤價
# ══════════════════════════════════════════════════════

def fetch_prices() -> dict[str, dict]:
    print("[1/3] 抓取收盤價（STOCK_DAY_ALL）…")
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = http_get(url)
    if not r:
        print("  [WARN] 無法取得收盤價（可能遭 403 封鎖），嘗試讀取快取…")
        return load_prices_cache()
    try:
        data = r.json()
    except Exception as e:
        print(f"  [WARN] JSON 解析失敗：{e}，嘗試讀取快取…")
        return load_prices_cache()

    result = {}
    for row in (data if isinstance(data, list) else []):
        code  = str(row.get("Code", "")).strip()
        name  = str(row.get("Name", "")).strip()
        close = to_float(row.get("ClosingPrice", 0))
        tvol  = to_float(row.get("TradeVolume",  0))
        tval  = to_float(row.get("TradeValue",   0))
        if is_listed(code) and close > 0:
            result[code] = {"name": name, "close": close,
                            "trade_volume": tvol, "trade_value": tval}

    if result:
        save_prices_cache(result)
        print(f"  → 取得 {len(result)} 支（已更新快取）")
    else:
        print("  [WARN] 收盤價資料為空，嘗試讀取快取…")
        result = load_prices_cache()
    return result


# ══════════════════════════════════════════════════════
# Step 2：市值計算
# ══════════════════════════════════════════════════════

def fetch_bwibbu() -> dict[str, float]:
    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    r = http_get(url)
    if not r:
        return {}
    try:
        data = r.json()
        result = {}
        for row in (data if isinstance(data, list) else []):
            code = str(row.get("Code", "")).strip()
            pb   = to_float(row.get("PBratio", 0))
            if is_listed(code) and pb > 0:
                result[code] = pb
        print(f"  [市值-B] BWIBBU_ALL 取得 {len(result)} 筆 PBratio")
        return result
    except Exception:
        return {}


def load_shares_cache() -> dict:
    """讀取上次成功抓取的發行股數快取（7 天內有效）"""
    try:
        if CACHE_SHARES_PATH.exists():
            obj = json.loads(CACHE_SHARES_PATH.read_text(encoding="utf-8"))
            # 版本不符（舊版未扣庫藏股）→ 丟棄並強制重新抓取
            if obj.get("_version") != CACHE_SHARES_VERSION:
                print("  [快取] 發行股數快取版本不符（舊版未扣庫藏股），強制更新…")
                return {}
            age_h = (time.time() - obj.get("_ts", 0)) / 3600
            if age_h < 168:  # 7 天（發行股數變動不頻繁）
                print(f"  [快取] 使用 {age_h:.1f} 小時前的發行股數快取")
                return obj.get("shares", {})
            else:
                print(f"  [快取] 發行股數快取已過期（{age_h:.1f} 小時），略過")
    except Exception as e:
        print(f"  [快取] 發行股數讀取失敗：{e}")
    return {}


def save_shares_cache(shares: dict):
    try:
        CACHE_SHARES_PATH.parent.mkdir(parents=True, exist_ok=True)
        obj = {"_ts": time.time(), "_version": CACHE_SHARES_VERSION, "shares": shares}
        CACHE_SHARES_PATH.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  [快取] 發行股數儲存失敗：{e}")


def fetch_shares() -> dict[str, float]:
    """從 TWSE t187ap03_L 取得各上市公司總發行股數（股）。
    API 封鎖時自動讀取快取（有效期 7 天）。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    r = http_get(url)
    if not r:
        print("  [WARN] t187ap03_L 無法取得，嘗試讀取快取…")
        return load_shares_cache()
    try:
        data = r.json()
        if not isinstance(data, list) or not data:
            print("  [WARN] t187ap03_L 回傳空資料，嘗試讀取快取…")
            return load_shares_cache()

        # 偵測實際欄位名稱（方便除錯）
        first_keys = list(data[0].keys()) if data else []
        print(f"  [除錯] t187ap03_L 欄位：{first_keys}")

        # 嘗試多種可能的欄位名稱（千股 / 仟股，全角 / 半角括號）
        SHARES_KEYS = [
            "普通股發行股數（千股）",   # 全角括號 + 千
            "普通股發行股數(千股)",     # 半角括號 + 千
            "普通股發行股數（仟股）",   # 全角括號 + 仟
            "普通股發行股數(仟股)",     # 半角括號 + 仟
            "普通股(千股)",
            "普通股(仟股)",
        ]
        # 庫藏股欄位（已買回尚未銷除，應從發行股數中扣除）
        TREASURY_KEYS = [
            "庫藏股-普通股（千股）",
            "庫藏股-普通股(千股)",
            "庫藏股普通股（千股）",
            "庫藏股普通股(千股)",
            "庫藏股（千股）",
            "庫藏股(千股)",
        ]
        # 動態偵測：從實際欄位中找包含「普通股」「庫藏股」與「股」的欄位
        for k in first_keys:
            if "普通股" in k and ("千股" in k or "仟股" in k or "股數" in k):
                if k not in SHARES_KEYS:
                    SHARES_KEYS.insert(0, k)
                    print(f"  [除錯] 動態偵測到發行股數欄位：{k}")
            if "庫藏股" in k and ("千股" in k or "仟股" in k):
                if k not in TREASURY_KEYS:
                    TREASURY_KEYS.insert(0, k)
                    print(f"  [除錯] 動態偵測到庫藏股欄位：{k}")

        result = {}
        matched_key = None
        treasury_key = None
        for row in data:
            code = str(row.get("公司代號", "")).strip()
            if not is_listed(code):
                continue
            for k in SHARES_KEYS:
                val = to_float(row.get(k, 0))
                if val > 0:
                    total = val * 1000  # 千股 → 股
                    # 扣除庫藏股，得到流通在外股數（更接近實際市值）
                    treasury = 0.0
                    for tk in TREASURY_KEYS:
                        tv = to_float(row.get(tk, 0))
                        if tv > 0:
                            treasury = tv * 1000
                            if treasury_key is None:
                                treasury_key = tk
                            break
                    net = total - treasury if treasury > 0 else total
                    result[code] = max(net, total * 0.01)  # 最少保留1%防止資料異常
                    if matched_key is None:
                        matched_key = k
                        print(f"  [市值-A] 使用欄位：{k}"
                              + (f"，庫藏股欄位：{treasury_key}" if treasury_key else "（無庫藏股資料）"))
                    break

        if result:
            tsmc = result.get("2330", 0)
            # ── 合理性驗證：台積電實際流通約 259 億股（2.59×10¹⁰）──────────
            # 情況 A：股數 < 10億  → 某欄位讀錯，資料不可用
            if tsmc > 0 and tsmc < 1_000_000_000:
                print(f"  [WARN] 台積電股數偏少（{tsmc:,}股），資料有誤，清除快取")
                return load_shares_cache()
            # 情況 B：股數 > 5000億 → API 已回傳全股（股），×1000 過度膨脹，自動 ÷1000
            if tsmc > 500_000_000_000:
                print(f"  [WARN] 台積電股數（{tsmc:,.0f}）偏多，"
                      f"API 回傳單位為「股」而非「千股」，自動 ÷1000 修正…")
                result = {k: round(v / 1000) for k, v in result.items()}
                print(f"  [市值-A] 修正後台積電股數：{result.get('2330', 0):,.0f} 股")
            save_shares_cache(result)
            print(f"  [市值-A] t187ap03_L 取得 {len(result)} 支（已更新快取）")
        else:
            print(f"  [WARN] t187ap03_L 無法匹配發行股數欄位（欄位列表：{first_keys}），嘗試讀取快取…")
            result = load_shares_cache()
        return result
    except Exception as e:
        print(f"  [WARN] fetch_shares：{e}，嘗試讀取快取…")
        return load_shares_cache()


def build_top100(prices: dict) -> list[dict]:
    print("[2/3] 計算市值排名…")

    # ── Method A：收盤價 × t187ap03_L 總發行股數（正確市值）────────────
    total_shares = fetch_shares()

    if total_shares:
        rows = []
        for code, p in prices.items():
            shares = total_shares.get(code, 0)
            if shares <= 0:
                continue
            mv = p["close"] * shares
            rows.append({
                "code": code, "name": p["name"],
                "close": p["close"], "market_cap": mv,
                "market_cap_method": "A",
            })
        if rows:
            rows.sort(key=lambda x: x["market_cap"], reverse=True)
            for i, r in enumerate(rows, 1):
                r["rank"] = i
            print(f"  → [方法A] t187ap03_L 上市普通股 {len(rows)} 支，取前 {min(len(rows), TOP_N)} 大")
            return rows[:TOP_N]
        print("  [WARN] Method A：shares 有資料但無股票通過篩選，嘗試 Method B…")
    else:
        print("  [WARN] t187ap03_L 無資料，嘗試 Method B（PBratio 代理估算）…")

    # ── Method B：ClosingPrice² ÷ PBratio（代理排名）────────────────────
    bwibbu = fetch_bwibbu()
    if bwibbu:
        rows = []
        for code, p in prices.items():
            pb = bwibbu.get(code, 0)
            if pb <= 0:
                continue
            mv = p["close"] ** 2 / pb  # proxy: Price × BookValue/share
            rows.append({
                "code": code, "name": p["name"],
                "close": p["close"], "market_cap": mv,
                "market_cap_method": "B",
            })
        if rows:
            rows.sort(key=lambda x: x["market_cap"], reverse=True)
            for i, r in enumerate(rows, 1):
                r["rank"] = i
            print(f"  → [方法B] PBratio 代理估算 {len(rows)} 支，取前 {min(len(rows), TOP_N)} 大（⚠️ 非精確市值）")
            return rows[:TOP_N]
        print("  [WARN] Method B：BWIBBU 有資料但無股票通過篩選，嘗試 Method C…")
    else:
        print("  [WARN] Method B 資料不足，嘗試 Method C（交易金額排名）…")

    # ── Method C：TradeValue 排名（最後備援）────────────────────────────
    rows = []
    for code, p in prices.items():
        tv = p.get("trade_value", 0)
        if tv <= 0:
            continue
        rows.append({
            "code": code, "name": p["name"],
            "close": p["close"], "market_cap": tv,
            "market_cap_method": "C",
        })
    if rows:
        rows.sort(key=lambda x: x["market_cap"], reverse=True)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        print(f"  → [方法C] 交易金額排名 {len(rows)} 支，取前 {min(len(rows), TOP_N)} 大（⚠️ 僅供參考）")
        return rows[:TOP_N]

    print("  [ERROR] 所有市值估算方法均失敗")
    return []


# ══════════════════════════════════════════════════════
# Step 3：0050 成份股
# ══════════════════════════════════════════════════════

def load_components_csv() -> dict[str, tuple[str, int, float]]:
    """從 0050_component_paste.txt 讀取成份股（格式同 LATEST_0050）。
    檔案為 tab 分隔，欄位：商品代碼\t商品名稱\t商品數量\t商品權重
    最後更新日期以檔案修改時間為準。"""
    if not COMP_CSV_PATH.exists():
        return {}
    try:
        import csv
        result = {}
        with open(COMP_CSV_PATH, newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                code = str(row.get('商品代碼', '')).strip()
                name = str(row.get('商品名稱', '')).strip()
                qty  = int(float(str(row.get('商品數量', '0')).replace(',', '')))
                wt   = float(str(row.get('商品權重', '0')).replace('%', '').replace(',', ''))
                if is_listed(code) and name:
                    result[code] = (name, qty, wt)
        if result:
            mtime = datetime.fromtimestamp(COMP_CSV_PATH.stat().st_mtime).strftime('%Y-%m-%d')
            print(f"  [TXT] 成份股載入 {len(result)} 檔（檔案更新時間：{mtime}）")
        return result
    except Exception as e:
        print(f"  [WARN] 讀取 0050_component_paste.txt 失敗：{e}")
        return {}


def fetch_0050(csv_data: dict | None = None) -> tuple[dict[str, str], str]:
    """回傳 (成份股dict, 來源說明)。來源說明用於 HTML 顯示。"""
    print("[3/3] 抓取 0050 成份股…")

    # 只使用 TWSE 官方 ETF API（對 GitHub Actions 友善，不受反爬蟲封鎖）
    # 移除 wantgoo / yuanta — 這兩個來源在 GitHub Actions 常遭 403，
    # 且 wantgoo HTML 正規表達式容易解析錯誤，導致清單出錯。
    for delta in range(5):
        d = date.today() - timedelta(days=delta)
        if d.weekday() >= 5:
            continue
        try:
            r3 = http_get(
                f"https://www.twse.com.tw/fund/TWT38U"
                f"?response=json&date={d.strftime('%Y%m%d')}&stockNo=0050")
            if r3:
                data = r3.json()
                if data and data.get("data"):
                    result = {
                        str(row[0]).strip(): str(row[1]).strip()
                        for row in data["data"]
                        if len(row) >= 2 and is_listed(str(row[0]).strip())
                    }
                    if len(result) >= 40:
                        print(f"  → TWSE ETF API ({d}) 取得 {len(result)} 檔")
                        return result, f"TWSE ETF API（{d}）"
        except Exception as e:
            print(f"  [WARN] TWSE ETF API ({d})：{e}")
        time.sleep(0.3)

    # CSV 備援（比 hardcode 更容易更新，以檔案修改時間為最後更新日期）
    if csv_data and len(csv_data) >= 40:
        mtime = datetime.fromtimestamp(COMP_CSV_PATH.stat().st_mtime).strftime('%Y-%m-%d')
        simple = {k: v[0] for k, v in csv_data.items()}
        print(f"  → 使用本地 CSV 名單（{len(simple)} 檔，最後更新：{mtime}）")
        return simple, f"本地CSV（最後更新：{mtime}）"

    print(f"  → CSV 不可用，使用 hardcode 名單（{len(FALLBACK_0050)} 檔，資料日期：2026-05-08）")
    return FALLBACK_0050.copy(), "hardcode（最後更新：2026-05-08，元大投信）"


# ══════════════════════════════════════════════════════
# Step 4：異動分析
# ══════════════════════════════════════════════════════

def analyze(top100: list[dict], comp0050: dict[str, str], comp_source: str = "",
            comp_data: dict | None = None, prices: dict | None = None) -> dict:
    _comp_data = comp_data if comp_data else LATEST_0050
    _prices    = prices or {}
    rank_map   = {s["code"]: s["rank"] for s in top100}
    close_map  = {s["code"]: s["close"] for s in top100}
    mv_map     = {s["code"]: s["market_cap"] for s in top100}
    comp_codes = set(comp0050)
    additions, deletions = [], []

    for s in top100:
        if s["rank"] <= ADD_THRESHOLD and s["code"] not in comp_codes:
            additions.append({
                "type": "Add", "rank": s["rank"],
                "code": s["code"], "name": s["name"],
                "market_cap": s["market_cap"],
                "reason": f"市值排名第{s['rank']}，尚未在0050",
            })

    for code, name in comp0050.items():
        rank = rank_map.get(code)
        if rank is None:
            deletions.append({
                "type": "Delete", "rank": 999,
                "code": code, "name": name, "market_cap": 0,
                "reason": "市值排名已落出前100",
            })
        elif rank >= DEL_THRESHOLD:
            mv = next((s["market_cap"] for s in top100 if s["code"] == code), 0)
            deletions.append({
                "type": "Delete", "rank": rank,
                "code": code, "name": name, "market_cap": mv,
                "reason": f"市值排名第{rank}（≥{DEL_THRESHOLD}觸發）",
            })

    additions.sort(key=lambda x: x["rank"])
    deletions.sort(key=lambda x: x["rank"])
    add_codes = {a["code"] for a in additions}
    del_codes = {d["code"] for d in deletions}

    # ── 估算 0050 規模（AUM）：Σ 收盤價 × ETF 持倉股數 ──────────────
    # 優先使用 _prices（涵蓋所有上市股），top100 close_map 備援
    def _close(code: str) -> float:
        if code in _prices:
            return _prices[code].get("close", 0)
        return close_map.get(code, 0)

    aum_0050 = sum(
        _close(c) * _comp_data.get(c, LATEST_0050.get(c, ("", 0, 0.0)))[1]
        for c in comp_codes
        if _close(c) > 0
    )
    print(f"  [AUM] 估算 0050 規模：{aum_0050/1e8:.0f} 億元")

    # ── 異動後新成份股組合市值，供預估新進成員權重使用 ────────────────
    new_comp_codes = (comp_codes - del_codes) | add_codes
    total_mv_new50 = sum(mv_map.get(c, 0) for c in new_comp_codes)

    # ── 為 additions 加入預估權重與買進張數 ─────────────────────────
    # 預估權重 = 該股市值 / 新50檔市值加總
    # 預估買進張數 = 預估權重 × AUM / 股價 / 1000
    for a in additions:
        code  = a["code"]
        close = close_map.get(code, 0)
        mv    = mv_map.get(code, 0)
        est_w    = (mv / total_mv_new50 * 100) if total_mv_new50 > 0 and mv > 0 else 0.0
        est_lots = int(est_w / 100 * aum_0050 / close / 1000) if close > 0 and aum_0050 > 0 else 0
        a["est_weight"] = round(est_w, 2)
        a["est_lots"]   = est_lots

    # ── 為 deletions 加入現有持倉張數（預估賣出量）──────────────────
    # 持倉張數直接取 comp_data 商品數量（ETF 實際持股股數）÷ 1000
    for d in deletions:
        code = d["code"]
        rec  = _comp_data.get(code, LATEST_0050.get(code, ("", 0, 0.0)))
        qty  = rec[1] if len(rec) > 1 else 0
        wt   = rec[2] if len(rec) > 2 else 0.0
        d["est_weight"] = wt
        d["est_lots"]   = int(qty / 1000) if qty > 0 else 0

    for s in top100:
        s["in_0050"] = s["code"] in comp_codes
        s["change"]  = ("Add" if s["code"] in add_codes else
                        "Delete" if s["code"] in del_codes else "")

    comp_list = [{
        "code": c, "name": n,
        "rank": rank_map.get(c, 999),
        "weight": _comp_data.get(c, LATEST_0050.get(c, ("", 0, 0.0)))[2],
        "market_cap": next((s["market_cap"] for s in top100 if s["code"] == c), 0),
        "change": "Delete" if c in del_codes else "",
    } for c, n in comp0050.items()]
    comp_list.sort(key=lambda x: x["rank"])

    print(f"  → 可能列入 {len(additions)} 檔，可能踢除 {len(deletions)} 檔")
    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "build_ts": int(datetime.now().timestamp()),   # 用於 HTML 強制刷新
        "top100": top100, "components": comp_list,
        "additions": additions, "deletions": deletions,
        "comp_source": comp_source,
        "aum_0050": aum_0050,
        "summary": {
            "top100_count": len(top100),
            "component_count": len(comp0050),
            "add_count": len(additions),
            "del_count": len(deletions),
        },
    }


# ══════════════════════════════════════════════════════
# Step 5：HTML 產生
# ══════════════════════════════════════════════════════

def fmt_cap(v: float) -> str:
    if v >= 1e12: return f"{v/1e12:.2f} 兆"
    if v >= 1e8:  return f"{v/1e8:.0f} 億"
    if v >= 1e6:  return f"{v/1e6:.0f} 百萬"
    return f"{v:,.0f}"


def fmt_lots(v: int) -> str:
    """格式化張數：1張＝1000股"""
    if v <= 0:     return "—"
    if v >= 10000: return f"{v/10000:.1f}萬張"
    if v >= 1000:  return f"{v/1000:.1f}千張"
    return f"{v:,}張"


def build_html(result: dict) -> str:
    updated      = result["updated_at"]
    build_ts     = result["build_ts"]
    s            = result["summary"]
    comp_source  = result.get("comp_source", "")
    is_hardcode  = "hardcode" in comp_source
    aum_0050     = result.get("aum_0050", 0)
    adds     = result["additions"]
    dels     = result["deletions"]
    top100   = result["top100"]
    comps    = result["components"]
    changes  = sorted(adds + dels,
                      key=lambda x: (0 if x["type"] == "Add" else 1, x["rank"]))

    def chg_rows():
        if not changes:
            return ('<tr><td colspan="8" style="text-align:center;'
                    'padding:2rem;color:#999">目前無異動建議</td></tr>')
        out = ""
        for c in changes:
            strong = ((c["type"] == "Add"    and c["rank"] <= 40) or
                      (c["type"] == "Delete" and c["rank"] >= 60))
            if c["type"] == "Add":
                b = ('<span class="badge badge-add-strong">▲ 強力列入 Add ★</span>'
                     if strong else
                     '<span class="badge badge-add">▲ 列入 Add</span>')
                row_sty  = ' style="background:#dcfce7"' if strong else ''
                lots_sty = 'color:#276749;font-weight:600'
            else:
                b = ('<span class="badge badge-del-strong">▼ 強力踢除 Del ★</span>'
                     if strong else
                     '<span class="badge badge-del">▼ 踢除 Del</span>')
                row_sty  = ' style="background:#fee2e2"' if strong else ''
                lots_sty = 'color:#9b2c2c;font-weight:600'
            r_s  = f"#{c['rank']}" if c["rank"] < 999 else "#100+"
            cap  = fmt_cap(c["market_cap"]) if c["market_cap"] else "—"
            wt   = f'{c.get("est_weight", 0):.2f}%' if c.get("est_weight") else "—"
            lots = fmt_lots(c.get("est_lots", 0))
            out += (f'<tr{row_sty}><td>{b}</td><td class="rank">{r_s}</td>'
                    f'<td><span class="code">{c["code"]}</span></td>'
                    f'<td>{c["name"]}</td>'
                    f'<td style="text-align:right;font-family:monospace">{cap}</td>'
                    f'<td style="text-align:right">{wt}</td>'
                    f'<td style="text-align:right;{lots_sty}">{lots}</td>'
                    f'<td style="font-size:11px;color:#666">{c["reason"]}</td></tr>\n')
        return out

    def t100_rows():
        out = ""
        for r in top100:
            b0 = ('<span class="badge badge-in">✓</span>'
                  if r["in_0050"] else '<span style="color:#ccc">—</span>')
            bc = (('<span class="badge badge-add" style="font-size:11px">+列入</span>'
                   if r["change"] == "Add" else
                   '<span class="badge badge-del" style="font-size:11px">-踢除</span>')
                  if r["change"] else "")
            cap = fmt_cap(r["market_cap"])
            out += (f'<tr data-s="{r["code"]} {r["name"]}">'
                    f'<td class="rank">#{r["rank"]}</td>'
                    f'<td><span class="code">{r["code"]}</span></td>'
                    f'<td>{r["name"]}</td>'
                    f'<td style="text-align:right;font-family:monospace">{r["close"]:,.0f}</td>'
                    f'<td style="text-align:right;font-family:monospace">{cap}</td>'
                    f'<td style="text-align:center">{b0}</td>'
                    f'<td style="text-align:center">{bc}</td></tr>\n')
        return out

    def comp_rows():
        out = ""
        for c in comps:
            bc   = ('<span class="badge badge-del" style="font-size:11px">-踢除</span>'
                    if c["change"] == "Delete" else "")
            rank = f"#{c['rank']}" if c["rank"] < 999 else "100名外"
            cap  = fmt_cap(c["market_cap"]) if c["market_cap"] else "—"
            wt   = f'{c["weight"]:.2f}%' if c.get("weight") else "—"
            sty  = ' style="background:#fff5f5"' if c["change"] else ""
            out += (f'<tr data-s="{c["code"]} {c["name"]}"{sty}>'
                    f'<td><span class="code">{c["code"]}</span></td>'
                    f'<td>{c["name"]}</td>'
                    f'<td class="rank">{rank}</td>'
                    f'<td style="text-align:right;font-family:monospace">{cap}</td>'
                    f'<td style="text-align:right">{wt}</td>'
                    f'<td style="text-align:center">{bc}</td></tr>\n')
        return out

    css = """:root{--g:#276749;--gb:#f0fff4;--ge:#9ae6b4;--r:#9b2c2c;--rb:#fff5f5;--re:#fed7d7;
      --b:#2b6cb0;--bb:#ebf8ff;--bo:#e2e8f0;--bg:#f7fafc;--tx:#2d3748;--gx:#4a5568}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);font-size:14px}
header{background:#1a202c;color:#fff;padding:1.25rem 2rem}
header h1{font-size:18px;font-weight:600;margin-bottom:3px}
header p{font-size:12px;color:#a0aec0;line-height:1.6}
.wrap{max-width:1150px;margin:0 auto;padding:1.25rem 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:1.25rem}
.card{background:#fff;border:1px solid var(--bo);border-radius:8px;padding:.9rem 1rem}
.lbl{font-size:11px;color:var(--gx);margin-bottom:3px}.val{font-size:24px;font-weight:600}
.val.g{color:var(--g)}.val.r{color:var(--r)}
.notice{background:var(--bb);border-left:4px solid var(--b);border-radius:0 6px 6px 0;
        padding:9px 13px;font-size:13px;color:var(--b);margin-bottom:1.25rem;line-height:1.7}
.tabs{display:flex;border-bottom:2px solid var(--bo);margin-bottom:1rem}
.tab{padding:8px 15px;font-size:13px;font-weight:500;cursor:pointer;
     border-bottom:2px solid transparent;margin-bottom:-2px;color:var(--gx);transition:all .15s}
.tab.on{color:#1a202c;border-bottom-color:#1a202c}
.pane{display:none}.pane.on{display:block}
.search{width:100%;padding:7px 10px;border:1px solid var(--bo);border-radius:6px;
        font-size:13px;margin-bottom:8px;background:#fff;color:var(--tx)}
.tw{border:1px solid var(--bo);border-radius:8px;overflow:hidden}
table{width:100%;border-collapse:collapse}
th{background:var(--bg);font-size:12px;font-weight:600;color:var(--gx);
   padding:8px 10px;text-align:left;border-bottom:1px solid var(--bo)}
td{padding:8px 10px;border-bottom:1px solid var(--bo);vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:#f7fafc}
.badge{display:inline-flex;align-items:center;gap:3px;font-size:12px;font-weight:500;
       padding:3px 8px;border-radius:99px;white-space:nowrap}
.badge-add{background:var(--gb);color:var(--g);border:1px solid var(--ge)}
.badge-del{background:var(--rb);color:var(--r);border:1px solid var(--re)}
.badge-add-strong{background:var(--g);color:#fff;border:1px solid var(--g);font-weight:700}
.badge-del-strong{background:var(--r);color:#fff;border:1px solid var(--r);font-weight:700}
.badge-in{background:#e6fffa;color:var(--g);font-size:11px;padding:2px 6px;border-radius:99px}
.rank{color:var(--b);font-weight:600;font-variant-numeric:tabular-nums}
.code{font-family:monospace;font-size:12px;background:var(--bg);padding:2px 5px;border-radius:4px}
footer{text-align:center;font-size:12px;color:#a0aec0;padding:1.5rem}
footer a{color:#a0aec0}
@media(max-width:600px){
  .grid{grid-template-columns:repeat(2,1fr)}
  th,td{padding:6px 7px;font-size:12px}.tab{padding:7px 10px;font-size:12px}
  .tw{overflow-x:auto;border-radius:6px}
  table{min-width:600px}}"""

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- 強制瀏覽器不快取，每次都抓最新版本 -->
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>台股 0050 成份股異動分析 | {updated}</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>📊 台股 0050 成份股異動分析</h1>
  <p>資料更新：{updated}　｜　Build：{build_ts}
  <br>市值來源：TWSE STOCK_DAY_ALL（收盤價）× t187ap03_L（公司總發行股數）　｜　成份股來源：{comp_source}</p>
</header>
<div class="wrap">
  <div class="notice">
    ⚠️ <strong>分析規則（富時羅素 FTSE Russell）</strong>：
    市值排名<strong>進入前 {ADD_THRESHOLD} 名</strong>且未在0050 → 可能列入；
    市值排名<strong>落至 {DEL_THRESHOLD} 名之後</strong>且已在0050 → 可能踢除。
    每季（3/6/9/12月）正式審核，以富時羅素公告為準。
  </div>
  {'<div class="notice" style="background:#fffbeb;border-left-color:#d97706;color:#92400e">⚠️ <strong>現有成份股資料來源：{comp_source}</strong>，非即時資料。TWSE API 不可用時自動啟用，成份股清單可能與最新不符，請留意。</div>' if is_hardcode else ''}
  <div class="grid">
    <div class="card"><div class="lbl">分析上市公司</div><div class="val">{s['top100_count']}</div></div>
    <div class="card"><div class="lbl">0050現有成份股</div><div class="val">{s['component_count']}</div></div>
    <div class="card"><div class="lbl">可能列入</div><div class="val g">{s['add_count']}</div></div>
    <div class="card"><div class="lbl">可能踢除</div><div class="val r">{s['del_count']}</div></div>
    <div class="card"><div class="lbl">0050規模估算</div><div class="val" style="font-size:18px">{fmt_cap(aum_0050)}</div></div>
  </div>
  <div class="tabs">
    <div class="tab on"  onclick="sw('chg')">📋 異動分析 ({s['add_count']+s['del_count']})</div>
    <div class="tab"     onclick="sw('t100')">🏆 市值前 {s['top100_count']}</div>
    <div class="tab"     onclick="sw('comp')">📦 現有成份股 ({s['component_count']})</div>
  </div>
  <div class="pane on" id="pane-chg">
    <div class="tw"><table>
      <thead><tr>
        <th style="width:110px">類型</th><th style="width:65px">排名</th>
        <th style="width:70px">代號</th><th>公司名稱</th>
        <th style="width:100px;text-align:right">市值</th>
        <th style="width:80px;text-align:right">預估權重</th>
        <th style="width:95px;text-align:right">預估張數</th>
        <th>原因</th>
      </tr></thead><tbody>{chg_rows()}</tbody>
    </table></div>
  </div>
  <div class="pane" id="pane-t100">
    <input class="search" placeholder="搜尋代號或公司名稱…" oninput="flt('tbt',this.value)">
    <div class="tw"><table>
      <thead><tr>
        <th style="width:50px">排名</th><th style="width:65px">代號</th><th>公司名稱</th>
        <th style="width:80px;text-align:right">收盤價</th>
        <th style="width:100px;text-align:right">市值</th>
        <th style="width:55px;text-align:center">0050</th>
        <th style="width:75px;text-align:center">異動</th>
      </tr></thead><tbody id="tbt">{t100_rows()}</tbody>
    </table></div>
  </div>
  <div class="pane" id="pane-comp">
    <input class="search" placeholder="搜尋代號或公司名稱…" oninput="flt('tbc',this.value)">
    <div class="tw"><table>
      <thead><tr>
        <th style="width:65px">代號</th><th>公司名稱</th>
        <th style="width:75px">排名</th>
        <th style="width:100px;text-align:right">市值</th>
        <th style="width:65px;text-align:right">權重</th>
        <th style="width:75px;text-align:center">異動</th>
      </tr></thead><tbody id="tbc">{comp_rows()}</tbody>
    </table></div>
  </div>
</div>
<footer>
  資料僅供參考，以富時羅素正式公告為準 ｜
  <a href="https://openapi.twse.com.tw" target="_blank">TWSE OpenAPI</a>　
  <a href="https://www.twse.com.tw/fund/TWT38U" target="_blank">TWSE ETF持股</a>
</footer>
<script>
const T=['chg','t100','comp'];
function sw(n){{
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('on',T[i]===n));
  T.forEach(k=>document.getElementById('pane-'+k).classList.toggle('on',k===n));
}}
function flt(id,q){{
  const lq=q.toLowerCase();
  document.getElementById(id).querySelectorAll('tr').forEach(r=>{{
    r.style.display=(!lq||(r.dataset.s||'').toLowerCase().includes(lq))?'':'none';
  }});
}}
</script>
</body></html>"""


# ══════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════

def main():
    print("=" * 55)
    print("台股 0050 成份股異動分析系統 v6.2")
    print(f"執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    prices = fetch_prices()
    if not prices:
        raise SystemExit("[ERROR] 無法取得收盤價，程式終止")

    time.sleep(1)
    top100 = build_top100(prices)
    if not top100:
        raise SystemExit("[ERROR] 所有市值估算方法（A/B/C）均失敗，程式終止")

    time.sleep(1)
    csv_data             = load_components_csv()
    comp0050, comp_source = fetch_0050(csv_data)
    comp_data            = csv_data if csv_data else LATEST_0050
    result               = analyze(top100, comp0050, comp_source, comp_data, prices)

    out = Path(__file__).parent.parent / "docs"
    out.mkdir(parents=True, exist_ok=True)

    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(
        build_html(result), encoding="utf-8")

    print("\n" + "=" * 55)
    print("✅ 完成！")
    print(f"  0050規模估算：{result.get('aum_0050', 0)/1e8:.0f} 億元")
    print(f"  可能列入：{result['summary']['add_count']} 檔")
    for a in result["additions"]:
        lots = a.get('est_lots', 0)
        wt   = a.get('est_weight', 0)
        print(f"    #{a['rank']:3d}  {a['code']} {a['name']}  預估買進 {lots:,}張（預估權重 {wt:.2f}%）")
    print(f"  可能踢除：{result['summary']['del_count']} 檔")
    for d in result["deletions"]:
        r    = str(d['rank']) if d['rank'] < 999 else "100+"
        lots = d.get('est_lots', 0)
        wt   = d.get('est_weight', 0)
        print(f"    #{r:>3}  {d['code']} {d['name']}  預估賣出 {lots:,}張（目前權重 {wt:.2f}%）")
    print(f"\n  ➜ docs/index.html（含 no-cache meta）")
    print(f"  ➜ docs/result.json")
    print("=" * 55)


if __name__ == "__main__":
    main()
