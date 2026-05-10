"""
台股 0050 成份股異動分析系統 v3.0
─────────────────────────────────────────────────────────
市值計算：
  收盤價來源：TWSE MI_INDEX?type=ALLBUT0999（個股盤後收盤行情）
              → 盤中/非交易日自動改抓 STOCK_DAY_ALL（最近月份）
  已發行股數：openapi.twse.com.tw /v1/opendata/t187ap03_L
              實收資本額(元) ÷ 10 = 股數
  市值 = 收盤價 × 股數

0050 成份股：
  A. 元大投信官網
  B. TWSE ETF 持股 API
  C. 備援硬編碼（2026-03-23 生效版）
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
    "Accept-Language": "zh-TW,zh;q=0.9",
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


def get_json(url: str, timeout: int = 30) -> dict | list | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
        r.raise_for_status()
        r.encoding = "utf-8"
        txt = r.text.strip()
        if not txt:
            print(f"  [WARN] 空回應：{url}")
            return None
        return json.loads(txt)
    except json.JSONDecodeError as e:
        print(f"  [WARN] JSON 解析失敗 {url}\n         → {e}")
        return None
    except Exception as e:
        print(f"  [WARN] 請求失敗 {url}\n         → {e}")
        return None


def to_float(s) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return 0.0


def is_listed_stock(code: str) -> bool:
    """只保留上市普通股：4碼純數字，首碼 1~9（非 0 開頭的 ETF）"""
    return bool(re.fullmatch(r"[1-9]\d{3}", code))


# ── Step 1：抓收盤價 ─────────────────────────────────────
def fetch_closing_prices() -> dict[str, dict]:
    """
    優先用 MI_INDEX（每日盤後個股行情），往前找最近 5 個交易日。
    type=ALLBUT0999 → 排除代號含 0999 的特殊商品，取得一般股票。
    data9 = 一般股票行情，fields9 = 對應欄位名稱。
    """
    print("[1/3] 抓取 TWSE MI_INDEX 個股收盤行情（type=ALLBUT0999）…")

    base_url = "https://www.twse.com.tw/exchangeReport/MI_INDEX"

    for delta in range(8):
        d = date.today() - timedelta(days=delta)
        if d.weekday() >= 5:        # 跳過週六日
            continue

        url = f"{base_url}?response=json&type=ALLBUT0999&date={d.strftime('%Y%m%d')}"
        print(f"  嘗試 {d} …", end=" ")
        data = get_json(url)

        if not data:
            print("無回應")
            time.sleep(0.5)
            continue

        stat = data.get("stat", "")
        if stat != "OK":
            print(f"stat={stat}（無資料）")
            time.sleep(0.5)
            continue

        fields = data.get("fields9", [])
        rows   = data.get("data9",   [])

        if not fields or not rows:
            print("欄位或資料為空")
            continue

        # 找欄位 index
        try:
            ic     = fields.index("證券代號")
            iname  = fields.index("證券名稱")
            iclose = fields.index("收盤價")
        except ValueError:
            print(f"欄位不符 fields9={fields[:5]}")
            continue

        result = {}
        for row in rows:
            code  = str(row[ic]).strip()
            name  = str(row[iname]).strip()
            close = to_float(row[iclose])
            if is_listed_stock(code) and close > 0:
                result[code] = {"name": name, "close": close}

        if result:
            print(f"✅ 取得 {len(result)} 支")
            return result
        print("無有效個股")

    # 備援：STOCK_DAY_ALL（全市場各股當月成交資訊 open_data 格式）
    return fetch_via_stock_day_all()


def fetch_via_stock_day_all() -> dict[str, dict]:
    """
    備援：STOCK_DAY_ALL?response=open_data
    欄位：證券代號, 證券名稱, ..., 收盤價(index=6)
    此端點每月更新一次（含該月最後收盤價），適合做備援。
    """
    print("  [備援] 嘗試 STOCK_DAY_ALL …")
    # 試當月與上個月
    for delta_months in [0, 1]:
        d = date.today().replace(day=1) - timedelta(days=delta_months * 28)
        url = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
               f"?response=open_data&date={d.strftime('%Y%m')}01")
        data = get_json(url)
        if not data:
            continue
        if isinstance(data, list) and len(data) > 1:
            result = {}
            for row in data[1:]:    # 第 0 列為欄位名稱
                if len(row) < 9:
                    continue
                code  = str(row[0]).strip()
                name  = str(row[1]).strip()
                close = to_float(row[6])     # 收盤價在 index 6
                if is_listed_stock(code) and close > 0:
                    result[code] = {"name": name, "close": close}
            if result:
                print(f"  → STOCK_DAY_ALL ({d.strftime('%Y-%m')}) 取得 {len(result)} 支")
                return result

    print("  [ERROR] 所有收盤價來源均失敗")
    return {}


# ── Step 2：已發行股數 ───────────────────────────────────
def fetch_shares() -> dict[str, float]:
    """
    openapi.twse.com.tw /v1/opendata/t187ap03_L
    欄位「實收資本額(元)」÷ 10 = 已發行普通股股數
    """
    print("[2/3] 抓取已發行股數（t187ap03_L）…")
    url  = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    data = get_json(url)
    if not data:
        return {}

    result = {}
    for row in data:
        code    = str(row.get("公司代號", "")).strip()
        capital = to_float(row.get("實收資本額(元)", 0))
        if is_listed_stock(code) and capital > 0:
            result[code] = capital / 10   # 股數
    print(f"  → 取得 {len(result)} 筆")
    return result


# ── Step 3：計算市值並排名 ────────────────────────────────
def build_top100(prices: dict, shares: dict) -> list[dict]:
    rows = []
    for code, p in prices.items():
        s = shares.get(code, 0)
        if s <= 0:
            continue
        mv = p["close"] * s
        rows.append({
            "code": code, "name": p["name"],
            "close": p["close"], "shares": s,
            "market_cap": mv,
        })

    rows.sort(key=lambda x: x["market_cap"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    total = len(rows)
    rows  = rows[:TOP_N]
    print(f"  → 共 {total} 支有效，取前 {len(rows)} 大")
    return rows


# ── Step 4：取 0050 成份股 ───────────────────────────────
def fetch_0050() -> dict[str, str]:
    print("[3/3] 抓取 0050 現有成份股…")

    # A. 元大投信官網
    try:
        r = requests.get(
            "https://www.yuantaetfs.com/product/detail/0050/ratio",
            headers=HEADERS, timeout=20, verify=False)
        r.encoding = "utf-8"
        for pat in [
            r'"constituents?"\s*:\s*(\[.*?\])',
            r'"stocks"\s*:\s*(\[.*?\])',
            r'"holdings"\s*:\s*(\[.*?\])',
        ]:
            m = re.search(pat, r.text, re.DOTALL | re.IGNORECASE)
            if m:
                stocks = json.loads(m.group(1))
                result = {}
                for s in stocks:
                    code = str(s.get("stockCode", s.get("code", ""))).strip()
                    name = str(s.get("stockName", s.get("name", ""))).strip()
                    if re.fullmatch(r"\d{4,}", code):
                        result[code] = name
                if len(result) >= 40:
                    print(f"  → 元大投信取得 {len(result)} 檔")
                    return result
    except Exception as e:
        print(f"  [WARN] 元大投信：{e}")

    # B. TWSE ETF 持股 API
    try:
        today = date.today().strftime("%Y%m%d")
        data  = get_json(
            f"https://www.twse.com.tw/fund/TWT38U"
            f"?response=json&date={today}&stockNo=0050")
        if data and data.get("data"):
            result = {}
            for row in data["data"]:
                if len(row) >= 2:
                    code = str(row[0]).strip()
                    name = str(row[1]).strip()
                    if is_listed_stock(code):
                        result[code] = name
            if len(result) >= 40:
                print(f"  → TWSE ETF API 取得 {len(result)} 檔")
                return result
    except Exception as e:
        print(f"  [WARN] TWSE ETF API：{e}")

    print(f"  → 使用備援名單（{len(FALLBACK_0050)} 檔，2026-03-23版）")
    return FALLBACK_0050.copy()


# ── Step 5：異動分析 ─────────────────────────────────────
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
                "reason": "排名已落出前100名",
            })
        elif rank >= DEL_THRESHOLD:
            mv = next((s["market_cap"] for s in top100 if s["code"] == code), 0)
            deletions.append({
                "type": "Delete", "rank": rank,
                "code": code, "name": name, "market_cap": mv,
                "reason": f"市值排名第{rank}名（≥{DEL_THRESHOLD}觸發）",
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

    print(f"  → 可能列入 {len(additions)} 檔，可能踢除 {len(deletions)} 檔")
    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "top100": top100, "components": comp_list,
        "additions": additions, "deletions": deletions,
        "summary": {
            "top100_count": len(top100),
            "component_count": len(comp0050),
            "add_count": len(additions),
            "del_count": len(deletions),
        },
    }


# ── Step 6：產生 HTML ─────────────────────────────────────
def fmt_cap(v: float) -> str:
    if v >= 1e12: return f"{v/1e12:.2f} 兆"
    if v >= 1e8:  return f"{v/1e8:.0f} 億"
    if v >= 1e6:  return f"{v/1e6:.0f} 百萬"
    return f"{v:,.0f}"


def build_html(result: dict) -> str:
    updated = result["updated_at"]
    s       = result["summary"]
    adds    = result["additions"]
    dels    = result["deletions"]
    top100  = result["top100"]
    comps   = result["components"]
    changes = sorted(adds + dels,
                     key=lambda x: (0 if x["type"] == "Add" else 1, x["rank"]))

    def chg_rows():
        if not changes:
            return '<tr><td colspan="6" style="text-align:center;padding:2rem;color:#999">目前無異動建議</td></tr>'
        out = ""
        for c in changes:
            b = ('<span class="badge badge-add">▲ 列入 Add</span>'
                 if c["type"] == "Add"
                 else '<span class="badge badge-del">▼ 踢除 Del</span>')
            r_s = f"#{c['rank']}" if c["rank"] < 999 else "#100+"
            cap = fmt_cap(c["market_cap"]) if c["market_cap"] else "—"
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
            out += (f'<tr data-s="{r["code"]} {r["name"]}">'
                    f'<td class="rank">#{r["rank"]}</td>'
                    f'<td><span class="code">{r["code"]}</span></td>'
                    f'<td>{r["name"]}</td>'
                    f'<td style="text-align:right;font-family:monospace">{r["close"]:,.0f}</td>'
                    f'<td style="text-align:right;font-family:monospace">{fmt_cap(r["market_cap"])}</td>'
                    f'<td style="text-align:center">{b0}</td>'
                    f'<td style="text-align:center">{bc}</td></tr>\n')
        return out

    def comp_rows():
        out = ""
        for c in comps:
            bc    = ('<span class="badge badge-del" style="font-size:11px">-踢除</span>'
                     if c["change"] == "Delete" else "")
            rank  = f"#{c['rank']}" if c["rank"] < 999 else "100名外"
            cap   = fmt_cap(c["market_cap"]) if c["market_cap"] else "—"
            style = ' style="background:#fff5f5"' if c["change"] else ""
            out  += (f'<tr data-s="{c["code"]} {c["name"]}"{style}>'
                     f'<td><span class="code">{c["code"]}</span></td>'
                     f'<td>{c["name"]}</td>'
                     f'<td class="rank">{rank}</td>'
                     f'<td style="text-align:right;font-family:monospace">{cap}</td>'
                     f'<td style="text-align:center">{bc}</td></tr>\n')
        return out

    css = """
:root{--g:#276749;--gb:#f0fff4;--ge:#9ae6b4;--r:#9b2c2c;--rb:#fff5f5;--re:#fed7d7;
      --b:#2b6cb0;--bb:#ebf8ff;--bo:#e2e8f0;--bg:#f7fafc;--tx:#2d3748;--gx:#4a5568}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);font-size:14px}
header{background:#1a202c;color:#fff;padding:1.25rem 2rem}
header h1{font-size:18px;font-weight:600;margin-bottom:3px}
header p{font-size:12px;color:#a0aec0;line-height:1.6}
.wrap{max-width:1150px;margin:0 auto;padding:1.25rem 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:1.25rem}
.card{background:#fff;border:1px solid var(--bo);border-radius:8px;padding:.9rem 1rem}
.lbl{font-size:11px;color:var(--gx);margin-bottom:3px}
.val{font-size:24px;font-weight:600}
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
footer{text-align:center;font-size:12px;color:#a0aec0;padding:1.5rem;margin-top:.5rem}
footer a{color:#a0aec0}
@media(max-width:600px){
  .grid{grid-template-columns:repeat(2,1fr)}
  th,td{padding:6px 7px;font-size:12px}
  .tab{padding:7px 10px;font-size:12px}
}"""

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股 0050 成份股異動分析</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>📊 台股 0050 成份股異動分析</h1>
  <p>資料更新：{updated}<br>
     市值 = TWSE 收盤價（MI_INDEX）× 已發行股數（t187ap03_L）｜成份股：元大投信 / TWSE</p>
</header>
<div class="wrap">
  <div class="notice">
    ⚠️ <strong>分析規則（富時羅素 FTSE Russell 指數邏輯）</strong>：
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
        <th style="width:90px;text-align:right">市值</th><th>原因</th>
      </tr></thead>
      <tbody>{chg_rows()}</tbody>
    </table></div>
  </div>
  <div class="pane" id="pane-t100">
    <input class="search" placeholder="搜尋代號或公司名稱…" oninput="flt('tbt',this.value)">
    <div class="tw"><table>
      <thead><tr>
        <th style="width:50px">排名</th><th style="width:65px">代號</th>
        <th>公司名稱</th>
        <th style="width:80px;text-align:right">收盤價</th>
        <th style="width:95px;text-align:right">市值</th>
        <th style="width:55px;text-align:center">0050</th>
        <th style="width:75px;text-align:center">異動</th>
      </tr></thead>
      <tbody id="tbt">{t100_rows()}</tbody>
    </table></div>
  </div>
  <div class="pane" id="pane-comp">
    <input class="search" placeholder="搜尋代號或公司名稱…" oninput="flt('tbc',this.value)">
    <div class="tw"><table>
      <thead><tr>
        <th style="width:65px">代號</th><th>公司名稱</th>
        <th style="width:75px">排名</th>
        <th style="width:95px;text-align:right">市值</th>
        <th style="width:75px;text-align:center">異動</th>
      </tr></thead>
      <tbody id="tbc">{comp_rows()}</tbody>
    </table></div>
  </div>
</div>
<footer>
  資料僅供參考，以富時羅素正式公告為準 ｜
  <a href="https://www.twse.com.tw" target="_blank">TWSE</a>　
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
</body>
</html>"""


# ── 主程式 ───────────────────────────────────────────────
def main():
    print("=" * 55)
    print("台股 0050 成份股異動分析系統 v3.0")
    print(f"執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    prices = fetch_closing_prices()
    if not prices:
        raise SystemExit("[ERROR] 無法取得收盤價，程式終止")

    time.sleep(1)
    shares = fetch_shares()
    if not shares:
        raise SystemExit("[ERROR] 無法取得發行股數，程式終止")

    top100 = build_top100(prices, shares)
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
