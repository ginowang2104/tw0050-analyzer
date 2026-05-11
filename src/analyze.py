"""
台股 0050 成份股異動分析系統 v5.0
─────────────────────────────────────────────────────────
市值計算策略（依序嘗試）：

  策略 A（最精確）：
    收盤價（STOCK_DAY_ALL.ClosingPrice）
    × 發行股數（t187ap03_L 成功時：各欄位自動探測）
    → 注意：t187ap03_L 在部分環境回 403，自動 fallback

  策略 B（備援，高精度）：
    利用 BWIBBU_ALL.PBratio 與 STOCK_DAY_ALL 交叉計算：
    每股淨值 = ClosingPrice / PBratio
    → 以「每股淨值 × ClosingPrice」作為市值代理（與真實市值
       高度正相關，足以排名）

  策略 C（最後備援）：
    直接用 STOCK_DAY_ALL.TradeValue（當日成交金額）排名
    大市值股票成交金額遠高於小市值，排名高度相關

0050 成份股：元大投信官網 → TWSE ETF API → 備援硬編碼
"""

import re
import csv
import json
import time
import requests
import urllib3
from datetime import datetime, date, timedelta
from io import StringIO
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
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.twse.com.tw/",
}

FALLBACK_0050 = {
    "2330":"台積電",  "2308":"台達電",  "2317":"鴻海",    "2454":"聯發科",
    "3711":"日月光投控","2891":"中信金","2882":"國泰金",  "2881":"富邦金",
    "2382":"廣達",    "2303":"聯電",    "3037":"欣興",    "2412":"中華電",
    "5880":"合庫金",  "2886":"兆豐金",  "2408":"南亞科",  "1303":"南亞",
    "6505":"台塑化",  "2885":"元大金",  "3034":"聯詠",    "6239":"力成",
    "2395":"研華",    "3045":"台灣大",  "2880":"華南金",  "1301":"台塑",
    "3008":"大立光",  "2327":"國巨",    "6669":"緯穎",    "2357":"華碩",
    "2376":"技嘉",    "1216":"統一",    "2890":"永豐金",  "2884":"玉山金",
    "2887":"台新金",  "2883":"開發金",  "2379":"瑞昱",    "4904":"遠傳",
    "2888":"新光金",  "2368":"金像電",  "2449":"京元電子","2344":"華邦電",
    "7769":"鴻勁精密","3231":"緯創",    "6770":"力積電",  "2474":"可成",
    "2615":"萬海",    "2609":"陽明",    "2801":"彰銀",    "5871":"中租-KY",
    "3443":"創意",    "2353":"宏碁",
}


def http_get(url: str, timeout: int = 30) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
        if r.status_code == 403:
            print(f"  [403] {url}")
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


# ══════════════════════════════════════════════════════
# 收盤價：STOCK_DAY_ALL（唯一確認可用）
# ══════════════════════════════════════════════════════

def fetch_price_and_volume() -> dict[str, dict]:
    """
    openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
    欄位：Code, Name, ClosingPrice, TradeVolume, TradeValue
    回傳 {code: {name, close, trade_volume, trade_value}}
    """
    print("[1/3] 抓取收盤價（STOCK_DAY_ALL）…")
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = http_get(url)
    if not r:
        return {}
    try:
        data = r.json()
    except Exception as e:
        print(f"  [WARN] JSON 解析失敗：{e}")
        return {}

    result = {}
    for row in (data if isinstance(data, list) else []):
        code  = str(row.get("Code", "")).strip()
        name  = str(row.get("Name", "")).strip()
        close = to_float(row.get("ClosingPrice", 0))
        tvol  = to_float(row.get("TradeVolume", 0))   # 當日成交股數
        tval  = to_float(row.get("TradeValue",  0))   # 當日成交金額（元）
        if is_listed(code) and close > 0:
            result[code] = {
                "name": name, "close": close,
                "trade_volume": tvol, "trade_value": tval,
            }
    print(f"  → 取得 {len(result)} 支上市股票")
    return result


# ══════════════════════════════════════════════════════
# 發行股數：策略 A — t187ap03_L（可能 403）
# ══════════════════════════════════════════════════════

def fetch_shares_t187() -> dict[str, float]:
    """
    嘗試從 t187ap03_L 取得發行股數。
    欄位名稱不確定（因環境無法測試），自動探測含「資本」或「股數」的欄位。
    """
    print("  [股數-A] 嘗試 t187ap03_L …")
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    r = http_get(url)
    if not r:
        return {}
    try:
        data = r.json()
    except Exception:
        return {}
    if not data:
        return {}

    # 探測欄位：印出第一筆所有欄位讓 log 可見
    sample = data[0]
    print(f"  [股數-A] t187ap03_L 第一筆欄位：{list(sample.keys())}")

    # 嘗試各種可能的欄位名稱
    capital_keys = [k for k in sample.keys()
                    if any(kw in k for kw in ["資本", "Capital", "capital", "股數", "Shares", "shares"])]
    code_keys    = [k for k in sample.keys()
                    if any(kw in k for kw in ["代號", "Code", "code", "股票"])]

    if not capital_keys or not code_keys:
        print(f"  [股數-A] 找不到資本/股數欄位，欄位清單：{list(sample.keys())}")
        return {}

    cap_key  = capital_keys[0]
    code_key = code_keys[0]
    print(f"  [股數-A] 使用欄位 code={code_key!r}, capital={cap_key!r}")

    result = {}
    for row in data:
        code    = str(row.get(code_key, "")).strip()
        capital = to_float(row.get(cap_key, 0))
        if is_listed(code) and capital > 0:
            # 若欄位是「實收資本額(元)」則除以 10；若已是股數則直接用
            val = capital / 10 if capital > 1e8 else capital
            result[code] = val

    print(f"  [股數-A] 取得 {len(result)} 筆")
    return result


# ══════════════════════════════════════════════════════
# 市值計算：整合三種策略
# ══════════════════════════════════════════════════════

def fetch_bwibbu() -> dict[str, float]:
    """
    openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL
    欄位：Code, PBratio（股價淨值比）
    回傳 {code: pb_ratio}
    """
    print("  [市值-B] 抓取 BWIBBU_ALL（PBratio）…")
    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    r = http_get(url)
    if not r:
        return {}
    try:
        data = r.json()
    except Exception:
        return {}
    result = {}
    for row in (data if isinstance(data, list) else []):
        code = str(row.get("Code", "")).strip()
        pb   = to_float(row.get("PBratio", 0))
        if is_listed(code) and pb > 0:
            result[code] = pb
    print(f"  [市值-B] 取得 {len(result)} 筆 PBratio")
    return result


def build_top100(prices: dict) -> list[dict]:
    """
    計算市值並排名，三種策略依序嘗試：
      A. 收盤價 × 發行股數（t187ap03_L）
      B. ClosingPrice² / PBratio（代理市值，∝ 真實市值）
      C. TradeValue（當日成交金額，流動性代理）
    """
    print("[2/3] 計算市值排名…")

    # 嘗試策略 A：發行股數
    shares  = fetch_shares_t187()
    pb_dict = {}
    method  = "C"

    if shares:
        method = "A"
        print(f"  → 策略 A：收盤價 × 發行股數（{len(shares)} 筆）")
    else:
        # 策略 B：PBratio
        pb_dict = fetch_bwibbu()
        if pb_dict:
            method = "B"
            print(f"  → 策略 B：ClosingPrice² ÷ PBratio（市值代理）")
        else:
            method = "C"
            print(f"  → 策略 C：TradeValue（當日成交金額）")

    rows = []
    for code, p in prices.items():
        if method == "A":
            s = shares.get(code, 0)
            if s <= 0:
                continue
            mv = p["close"] * s
        elif method == "B":
            pb = pb_dict.get(code, 0)
            if pb <= 0:
                continue
            # ClosingPrice² / PBratio = Price × (Price/PB) = Price × BookValuePerShare
            # ∝ 市值（假設同一市場淨值倍數分散程度相近）
            mv = (p["close"] ** 2) / pb
        else:
            mv = p["trade_value"]
            if mv <= 0:
                continue

        rows.append({
            "code": code, "name": p["name"],
            "close": p["close"],
            "market_cap": mv,
            "market_cap_method": method,
        })

    rows.sort(key=lambda x: x["market_cap"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    total = len(rows)
    result = rows[:TOP_N]
    print(f"  → {total} 支有效，取前 {len(result)} 大（方法：{method}）")
    return result


# ══════════════════════════════════════════════════════
# 0050 成份股
# ══════════════════════════════════════════════════════

def fetch_0050() -> dict[str, str]:
    print("[3/3] 抓取 0050 成份股…")

    # A. 元大投信官網
    try:
        r = requests.get(
            "https://www.yuantaetfs.com/product/detail/0050/ratio",
            headers=HEADERS, timeout=20, verify=False)
        r.encoding = "utf-8"
        for pat in [r'"constituents?"\s*:\s*(\[.*?\])',
                    r'"stocks"\s*:\s*(\[.*?\])',
                    r'"holdings"\s*:\s*(\[.*?\])']:
            m = re.search(pat, r.text, re.DOTALL | re.IGNORECASE)
            if m:
                items = json.loads(m.group(1))
                res   = {}
                for s in items:
                    code = str(s.get("stockCode", s.get("code", ""))).strip()
                    name = str(s.get("stockName", s.get("name", ""))).strip()
                    if re.fullmatch(r"\d{4,}", code):
                        res[code] = name
                if len(res) >= 40:
                    print(f"  → 元大投信 {len(res)} 檔")
                    return res
    except Exception as e:
        print(f"  [WARN] 元大投信：{e}")

    # B. TWSE ETF API
    try:
        r2 = requests.get(
            f"https://www.twse.com.tw/fund/TWT38U"
            f"?response=json&date={date.today().strftime('%Y%m%d')}&stockNo=0050",
            headers=HEADERS, timeout=20, verify=False)
        d2 = r2.json()
        if d2 and d2.get("data"):
            res = {str(row[0]).strip(): str(row[1]).strip()
                   for row in d2["data"]
                   if len(row) >= 2 and is_listed(str(row[0]).strip())}
            if len(res) >= 40:
                print(f"  → TWSE ETF API {len(res)} 檔")
                return res
    except Exception as e:
        print(f"  [WARN] TWSE ETF API：{e}")

    print(f"  → 使用備援名單 {len(FALLBACK_0050)} 檔")
    return FALLBACK_0050.copy()


# ══════════════════════════════════════════════════════
# 異動分析
# ══════════════════════════════════════════════════════

def analyze(top100: list[dict], comp0050: dict[str, str]) -> dict:
    rank_map   = {s["code"]: s["rank"] for s in top100}
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

    for s in top100:
        s["in_0050"] = s["code"] in comp_codes
        s["change"]  = ("Add" if s["code"] in add_codes else
                        "Delete" if s["code"] in del_codes else "")

    comp_list = [{
        "code": c, "name": n,
        "rank": rank_map.get(c, 999),
        "market_cap": next((s["market_cap"] for s in top100 if s["code"] == c), 0),
        "change": "Delete" if c in del_codes else "",
    } for c, n in comp0050.items()]
    comp_list.sort(key=lambda x: x["rank"])

    method = top100[0].get("market_cap_method", "?") if top100 else "?"
    print(f"  → 可能列入 {len(additions)} 檔，可能踢除 {len(deletions)} 檔")
    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market_cap_method": method,
        "top100": top100, "components": comp_list,
        "additions": additions, "deletions": deletions,
        "summary": {
            "top100_count": len(top100),
            "component_count": len(comp0050),
            "add_count": len(additions),
            "del_count": len(deletions),
        },
    }


# ══════════════════════════════════════════════════════
# HTML 產生
# ══════════════════════════════════════════════════════

def fmt_cap(v: float, method: str = "A") -> str:
    """市值格式化（A:元，B/C:相對數值）"""
    if method == "A":
        if v >= 1e12: return f"{v/1e12:.2f} 兆"
        if v >= 1e8:  return f"{v/1e8:.0f} 億"
        return f"{v/1e6:.0f} 百萬"
    return f"{v:,.0f}"


def build_html(result: dict) -> str:
    updated = result["updated_at"]
    method  = result.get("market_cap_method", "?")
    s       = result["summary"]
    adds    = result["additions"]
    dels    = result["deletions"]
    top100  = result["top100"]
    comps   = result["components"]
    changes = sorted(adds + dels,
                     key=lambda x: (0 if x["type"] == "Add" else 1, x["rank"]))

    method_desc = {
        "A": "收盤價 × 已發行股數（t187ap03_L）",
        "B": "收盤價² ÷ PBratio（BWIBBU_ALL，市值代理）",
        "C": "當日成交金額 TradeValue（流動性代理）",
    }.get(method, method)

    def chg_rows():
        if not changes:
            return ('<tr><td colspan="6" style="text-align:center;'
                    'padding:2rem;color:#999">目前無異動建議</td></tr>')
        out = ""
        for c in changes:
            b   = ('<span class="badge badge-add">▲ 列入 Add</span>'
                   if c["type"] == "Add"
                   else '<span class="badge badge-del">▼ 踢除 Del</span>')
            r_s = f"#{c['rank']}" if c["rank"] < 999 else "#100+"
            cap = fmt_cap(c["market_cap"], method) if c["market_cap"] else "—"
            out += (f'<tr><td>{b}</td><td class="rank">{r_s}</td>'
                    f'<td><span class="code">{c["code"]}</span></td>'
                    f'<td>{c["name"]}</td>'
                    f'<td style="text-align:right;font-family:monospace">{cap}</td>'
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
            cap = fmt_cap(r["market_cap"], method)
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
            cap  = fmt_cap(c["market_cap"], method) if c["market_cap"] else "—"
            sty  = ' style="background:#fff5f5"' if c["change"] else ""
            out += (f'<tr data-s="{c["code"]} {c["name"]}"{sty}>'
                    f'<td><span class="code">{c["code"]}</span></td>'
                    f'<td>{c["name"]}</td>'
                    f'<td class="rank">{rank}</td>'
                    f'<td style="text-align:right;font-family:monospace">{cap}</td>'
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
.badge-in{background:#e6fffa;color:var(--g);font-size:11px;padding:2px 6px;border-radius:99px}
.rank{color:var(--b);font-weight:600;font-variant-numeric:tabular-nums}
.code{font-family:monospace;font-size:12px;background:var(--bg);padding:2px 5px;border-radius:4px}
footer{text-align:center;font-size:12px;color:#a0aec0;padding:1.5rem}
footer a{color:#a0aec0}
@media(max-width:600px){
  .grid{grid-template-columns:repeat(2,1fr)}
  th,td{padding:6px 7px;font-size:12px}.tab{padding:7px 10px;font-size:12px}}"""

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股 0050 成份股異動分析</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>📊 台股 0050 成份股異動分析</h1>
  <p>資料更新：{updated}　｜　市值計算：{method_desc}</p>
</header>
<div class="wrap">
  <div class="notice">
    ⚠️ <strong>分析規則（富時羅素 FTSE Russell）</strong>：
    市值排名<strong>進入前 {ADD_THRESHOLD} 名</strong>且未在0050 → 可能列入；
    市值排名<strong>落至 {DEL_THRESHOLD} 名之後</strong>且已在0050 → 可能踢除。
    每季（3/6/9/12月）正式審核，以富時羅素公告為準。
  </div>
  <div class="grid">
    <div class="card"><div class="lbl">分析上市公司</div><div class="val">{s['top100_count']}</div></div>
    <div class="card"><div class="lbl">0050現有成份股</div><div class="val">{s['component_count']}</div></div>
    <div class="card"><div class="lbl">可能列入</div><div class="val g">{s['add_count']}</div></div>
    <div class="card"><div class="lbl">可能踢除</div><div class="val r">{s['del_count']}</div></div>
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
        <th style="width:100px;text-align:right">市值</th><th>原因</th>
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
        <th style="width:75px;text-align:center">異動</th>
      </tr></thead><tbody id="tbc">{comp_rows()}</tbody>
    </table></div>
  </div>
</div>
<footer>
  資料僅供參考，以富時羅素正式公告為準 ｜
  <a href="https://openapi.twse.com.tw" target="_blank">TWSE OpenAPI</a>　
  <a href="https://www.yuantaetfs.com/product/detail/0050/ratio" target="_blank">元大投信0050</a>
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
    print("台股 0050 成份股異動分析系統 v5.0")
    print(f"執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    prices = fetch_price_and_volume()
    if not prices:
        raise SystemExit("[ERROR] 無法取得收盤價，程式終止")

    time.sleep(1)
    top100 = build_top100(prices)
    if not top100:
        raise SystemExit("[ERROR] 市值計算失敗，程式終止")

    time.sleep(1)
    comp0050 = fetch_0050()
    result   = analyze(top100, comp0050)

    out = Path(__file__).parent.parent / "docs"
    out.mkdir(parents=True, exist_ok=True)

    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(
        build_html(result), encoding="utf-8")

    print("\n" + "=" * 55)
    print("✅ 完成！")
    method = result.get("market_cap_method", "?")
    method_desc = {"A":"收盤價×股數","B":"PBratio代理","C":"成交金額代理"}.get(method, method)
    print(f"  市值計算方法：{method}（{method_desc}）")
    print(f"  可能列入：{result['summary']['add_count']} 檔")
    for a in result["additions"]:
        print(f"    #{a['rank']:3d}  {a['code']} {a['name']}")
    print(f"  可能踢除：{result['summary']['del_count']} 檔")
    for d in result["deletions"]:
        r = str(d['rank']) if d['rank'] < 999 else "100+"
        print(f"    #{r:>3}  {d['code']} {d['name']}")
    print(f"\n  ➜ docs/index.html")
    print(f"  ➜ docs/result.json")
    print("=" * 55)


if __name__ == "__main__":
    main()
