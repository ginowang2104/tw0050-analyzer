
"""

台股 0050 成份股異動分析系統 v6.1

─────────────────────────────────────────────────────────

修正項目：

  1. 0050 hardcode 更新為 2026-05-08 最新資料（50檔）

  2. 0050 成份股抓取：優先 TWSE 官方 ETF API → hardcode

     （移除 wantgoo/yuanta，解決 GitHub Actions 403/解析錯誤）

  3. 市值計算：改用 TWSE t187ap03_L 取得公司總發行股數，

     不再誤用 0050 ETF 持倉量（商品數量）計算市值

  4. 403 備援：收盤價加入本地快取，封鎖時自動讀取上次成功資料

  5. index.html 加入時間戳 + no-cache meta，解決 GitHub Pages 快取問題

 

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

 

# 收盤價快取路徑（GitHub Actions 封鎖時，讀取上次成功的資料）

CACHE_PATH = Path(__file__).parent.parent / "docs" / ".cache_prices.json"

 

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

    return bool(re.fullmatch(r"[1-9]\d{3}", str(code).strip()))

 

# ── 收盤價本地快取（應對 403 封鎖）─────────────────────────

 

def load_prices_cache() -> dict:

    """讀取上次成功抓取的收盤價快取（48 小時內有效）"""

    try:

        if CACHE_PATH.exists():
