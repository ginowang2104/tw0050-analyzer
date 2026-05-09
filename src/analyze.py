"""
台股0050成份股異動分析系統
資料來源：
  - 市值排名：台灣證券交易所 opendata API
  - 0050成份股：元大投信官網 + TWSE
"""

import requests
import json
import time
import re
from datetime import datetime, date
from pathlib import Path


# ── 常數設定 ───────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 依規則：前42名可能列入，57名後可能踢除
ADD_THRESHOLD = 42
DEL_THRESHOLD = 57
TOP_N = 100  # 抓取前100大

# 已知0050成份股（2026年3月23日生效版）作為備援資料
FALLBACK_0050 = {
    "2330": "台積電", "2308": "台達電", "2317": "鴻海", "2454": "聯發科",
    "3711": "日月光投控", "2891": "中信金", "3037": "欣興", "2882": "國泰金",
    "2881": "富邦金", "2382": "廣達", "2303": "聯電", "2412": "中華電",
    "5880": "合庫金", "2886": "兆豐金", "2408": "南亞科", "1303": "南亞",
    "6505": "台塑化", "2885": "元大金", "3034": "聯詠", "6239": "力成",
    "2395": "研華", "3045": "台灣大", "2880": "華南金", "1301": "台塑",
    "3008": "大立光", "2327": "國巨", "6669": "緯穎", "2357": "華碩",
    "2376": "技嘉", "1216": "統一", "2890": "永豐金", "2884": "玉山金",
    "2887": "台新金", "2883": "開發金", "2379": "瑞昱", "4904": "遠傳",
    "2888": "新光金", "2368": "金像電", "2449": "京元電子", "2344": "華邦電",
    "7769": "鴻勁精密", "3231": "緯創", "6770": "力積電", "2474": "可成",
    "2615": "萬海", "2609": "陽明", "2801": "彰銀", "5871": "中租-KY",
    "3443": "創意", "2353": "宏碁",
}


# ── 工具函式 ───────────────────────────────────────────────
def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def safe_get(session: requests.Session, url: str, **kwargs) -> requests.Response | None:
    try:
        r = session.get(url, timeout=20, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [WARN] GET {url} 失敗：{e}")
        return None


# ── 資料抓取：市值排名 ─────────────────────────────────────
def fetch_market_cap_twse(session: requests.Session) -> list[dict]:
    """
    從 TWSE opendata 抓取上市公司市值，回傳已排序清單。
    端點：https://opendata.twse.com.tw/v1/opendata/t187ap03_L
    欄位：公司代號, 公司名稱, 市值(百萬元)
    """
    url = "https://opendata.twse.com.tw/v1/opendata/t187ap03_L"
    print(f"[1/3] 抓取TWSE市值資料…")
    r = safe_get(session, url)
    if not r:
        return []

    try:
        data = r.json()
    except Exception as e:
        print(f"  [WARN] JSON解析失敗：{e}")
        return []

    rows = []
    for item in data:
        code = item.get("公司代號", "").strip()
        name = item.get("公司名稱", "").strip()
        # 市值欄位名稱可能有不同版本
        mv_raw = (
            item.get("市值(百萬元)", "")
            or item.get("市值", "")
            or item.get("發行市值(元)", "")
            or "0"
        )
        mv_str = str(mv_raw).replace(",", "").strip()
        try:
            mv = float(mv_str) if mv_str else 0.0
        except ValueError:
            mv = 0.0

        # 過濾條件：
        # 1. 代號4碼純數字（上市普通股）
        # 2. 排除代號以 0 開頭（ETF）
        # 3. 市值 > 0
        if (
            re.fullmatch(r"\d{4}", code)
            and not code.startswith("0")
            and mv > 0
        ):
            rows.append({"code": code, "name": name, "market_cap": mv})

    # 依市值降冪排序，取前TOP_N
    rows.sort(key=lambda x: x["market_cap"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    print(f"  → 取得 {len(rows)} 筆上市公司（排除ETF後），取前{TOP_N}筆")
    return rows[:TOP_N]


# ── 備援：若 TWSE opendata 欄位有變，改抓 PChome 市值排行 ──
def fetch_market_cap_pchome(session: requests.Session) -> list[dict]:
    """
    備援資料源：PChome 股市市值排行
    """
    url = "https://pchome.megatime.com.tw/group/mkt5/cidE001.html"
    print(f"  [備援] 嘗試 PChome 市值排行…")
    r = safe_get(session, url)
    if not r:
        return []

    from html.parser import HTMLParser

    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows = []
            self._in_table = False
            self._cells = []
            self._in_td = False
            self._current = ""

        def handle_starttag(self, tag, attrs):
            attrs_d = dict(attrs)
            if tag == "tr":
                self._cells = []
            if tag in ("td", "th"):
                self._in_td = True
                self._current = ""
            if tag == "a" and self._in_td:
                href = attrs_d.get("href", "")
                m = re.search(r"sid(\d{4,6})", href)
                if m:
                    self._current = m.group(1)

        def handle_endtag(self, tag):
            if tag in ("td", "th"):
                self._cells.append(self._current.strip())
                self._in_td = False
                self._current = ""
            if tag == "tr" and len(self._cells) >= 3:
                self.rows.append(self._cells[:])
                self._cells = []

        def handle_data(self, data):
            if self._in_td:
                self._current += data

    p = TableParser()
    p.feed(r.text)

    rows = []
    for cells in p.rows:
        if len(cells) < 3:
            continue
        code = cells[0] if re.fullmatch(r"\d{4}", cells[0]) else ""
        name = cells[1] if len(cells) > 1 else ""
        mv_raw = cells[2].replace(",", "") if len(cells) > 2 else "0"
        if not code or code.startswith("0"):
            continue
        try:
            mv = float(mv_raw)
        except ValueError:
            mv = 0.0
        if mv > 0:
            rows.append({"code": code, "name": name, "market_cap": mv})

    rows.sort(key=lambda x: x["market_cap"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    print(f"  → 備援取得 {len(rows)} 筆")
    return rows[:TOP_N]


# ── 資料抓取：0050成份股 ───────────────────────────────────
def fetch_0050_components(session: requests.Session) -> dict[str, str]:
    """
    從元大投信官網抓取0050最新成份股。
    回傳 {股票代號: 公司名稱}
    """
    print(f"[2/3] 抓取0050成份股…")

    # 方法一：元大投信 ETF持股比重頁面
    url = "https://www.yuantaetfs.com/product/detail/0050/ratio"
    r = safe_get(session, url)
    components = {}

    if r:
        # 從 HTML 中找出持股資料（元大投信頁面用JS渲染，試抓script中的JSON）
        # 找 __NEXT_DATA__ 或類似的資料島
        m = re.search(r'"stocks"\s*:\s*(\[.*?\])', r.text, re.DOTALL)
        if m:
            try:
                stocks = json.loads(m.group(1))
                for s in stocks:
                    code = str(s.get("stockCode", "")).strip()
                    name = str(s.get("stockName", "")).strip()
                    if re.fullmatch(r"\d{4}", code):
                        components[code] = name
            except Exception:
                pass

    # 方法二：TWSE ETF持股明細 API
    if not components:
        today_str = date.today().strftime("%Y%m%d")
        url2 = f"https://www.twse.com.tw/fund/TWT38U?response=json&date={today_str}&stockNo=0050"
        r2 = safe_get(session, url2)
        if r2:
            try:
                d = r2.json()
                for row in d.get("data", []):
                    if len(row) >= 2:
                        code = row[0].strip()
                        name = row[1].strip()
                        if re.fullmatch(r"\d{4}", code):
                            components[code] = name
            except Exception:
                pass

    # 方法三：CMoney ETF持股頁
    if not components:
        url3 = "https://www.cmoney.tw/etf/tw/0050"
        r3 = safe_get(session, url3)
        if r3:
            matches = re.findall(
                r'"stockCode"\s*:\s*"(\d{4,6})".*?"stockName"\s*:\s*"([^"]+)"',
                r3.text,
            )
            for code, name in matches:
                if not code.startswith("0"):
                    components[code] = name

    if components:
        print(f"  → 取得 {len(components)} 檔0050成份股")
    else:
        print(f"  → 線上資料取得失敗，使用備援名單（{len(FALLBACK_0050)}檔）")
        components = FALLBACK_0050.copy()

    return components


# ── 核心分析邏輯 ───────────────────────────────────────────
def analyze(top100: list[dict], components: dict[str, str]) -> dict:
    """
    依規則計算可能列入/踢除名單。
    回傳結構化結果。
    """
    print("[3/3] 執行異動分析…")
    comp_codes = set(components.keys())
    rank_map = {r["code"]: r["rank"] for r in top100}

    additions = []   # 可能列入
    deletions = []   # 可能踢除

    # 可能列入：市值前42名 且 不在0050
    for stock in top100:
        if stock["rank"] <= ADD_THRESHOLD and stock["code"] not in comp_codes:
            additions.append({
                "type": "Add",
                "rank": stock["rank"],
                "code": stock["code"],
                "name": stock["name"],
                "market_cap": stock["market_cap"],
                "reason": f"市值排名第{stock['rank']}名，尚未列入0050",
            })

    # 可能踢除：已在0050 且 市值排名57名之後（含不在前100）
    for code, name in components.items():
        rank = rank_map.get(code)
        if rank is None:
            deletions.append({
                "type": "Delete",
                "rank": 999,
                "code": code,
                "name": name,
                "market_cap": 0,
                "reason": "市值排名已落出前100名",
            })
        elif rank >= DEL_THRESHOLD:
            cap = next((s["market_cap"] for s in top100 if s["code"] == code), 0)
            deletions.append({
                "type": "Delete",
                "rank": rank,
                "code": code,
                "name": name,
                "market_cap": cap,
                "reason": f"市值排名第{rank}名（已落至57名後）",
            })

    additions.sort(key=lambda x: x["rank"])
    deletions.sort(key=lambda x: x["rank"])

    # 為top100補充是否在0050的旗標
    for s in top100:
        s["in_0050"] = s["code"] in comp_codes
        s["change"] = (
            "Add" if any(a["code"] == s["code"] for a in additions) else
            "Delete" if any(d["code"] == s["code"] for d in deletions) else
            ""
        )

    # 為成份股補充市值排名
    comp_list = []
    for code, name in components.items():
        rank = rank_map.get(code, 999)
        cap = next((s["market_cap"] for s in top100 if s["code"] == code), 0)
        change = "Delete" if any(d["code"] == code for d in deletions) else ""
        comp_list.append({
            "code": code, "name": name,
            "rank": rank, "market_cap": cap, "change": change
        })
    comp_list.sort(key=lambda x: x["rank"])

    result = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "top100": top100,
        "components": comp_list,
        "additions": additions,
        "deletions": deletions,
        "summary": {
            "top100_count": len(top100),
            "component_count": len(components),
            "add_count": len(additions),
            "del_count": len(deletions),
        },
    }

    print(f"  → 可能列入：{len(additions)} 檔，可能踢除：{len(deletions)} 檔")
    return result


# ── HTML 報告產生 ───────────────────────────────────────────
def build_html(result: dict) -> str:
    updated = result["updated_at"]
    s = result["summary"]
    additions = result["additions"]
    deletions = result["deletions"]
    top100 = result["top100"]
    components = result["components"]
    changes = additions + deletions
    changes_sorted = sorted(changes, key=lambda x: (0 if x["type"]=="Add" else 1, x["rank"]))

    def fmt_cap(v):
        if v >= 1_000_000:
            return f"{v/1_000_000:.2f} 兆"
        elif v >= 1_000:
            return f"{v/1_000:.0f} 億"
        return f"{v:.0f} 百萬"

    def changes_rows():
        if not changes_sorted:
            return '<tr><td colspan="5" style="text-align:center;padding:2rem;color:#888">目前無異動建議</td></tr>'
        rows = ""
        for c in changes_sorted:
            badge = (
                '<span class="badge badge-add">▲ 列入 Add</span>'
                if c["type"] == "Add"
                else '<span class="badge badge-del">▼ 踢除 Del</span>'
            )
            cap_str = fmt_cap(c["market_cap"]) if c["market_cap"] else "—"
            rows += f"""<tr>
              <td>{badge}</td>
              <td class="rank">#{c['rank'] if c['rank'] < 999 else '100+'}</td>
              <td><span class="code">{c['code']}</span></td>
              <td>{c['name']}</td>
              <td style="font-size:12px;color:#666">{c['reason']}</td>
            </tr>"""
        return rows

    def top100_rows():
        rows = ""
        for s in top100:
            badge_0050 = '<span class="badge badge-in">✓ 是</span>' if s["in_0050"] else '<span style="color:#bbb;font-size:12px">否</span>'
            chg = ""
            if s["change"] == "Add":
                chg = '<span class="badge badge-add" style="font-size:11px">+列入</span>'
            elif s["change"] == "Delete":
                chg = '<span class="badge badge-del" style="font-size:11px">-踢除</span>'
            cap_str = fmt_cap(s["market_cap"])
            rows += f"""<tr>
              <td class="rank">#{s['rank']}</td>
              <td><span class="code">{s['code']}</span></td>
              <td>{s['name']}</td>
              <td style="text-align:right;font-family:monospace;font-size:12px">{cap_str}</td>
              <td style="text-align:center">{badge_0050}</td>
              <td style="text-align:center">{chg}</td>
            </tr>"""
        return rows

    def comp_rows():
        rows = ""
        for c in components:
            chg = ""
            if c["change"] == "Delete":
                chg = '<span class="badge badge-del" style="font-size:11px">-踢除</span>'
            rank_str = f"#{c['rank']}" if c["rank"] < 999 else "100名外"
            rank_cls = "rank" if c["rank"] < 999 else 'rank" style="color:#e53e3e'
            cap_str = fmt_cap(c["market_cap"]) if c["market_cap"] else "—"
            rows += f"""<tr>
              <td><span class="code">{c['code']}</span></td>
              <td>{c['name']}</td>
              <td class="{rank_cls}">{rank_str}</td>
              <td style="text-align:right;font-family:monospace;font-size:12px">{cap_str}</td>
              <td style="text-align:center">{chg}</td>
            </tr>"""
        return rows

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台股 0050 成份股異動分析</title>
<style>
  :root {{
    --green: #276749; --green-bg: #f0fff4; --green-border: #9ae6b4;
    --red: #9b2c2c;   --red-bg: #fff5f5;   --red-border: #fed7d7;
    --blue: #2b6cb0;  --blue-bg: #ebf8ff;
    --gray: #4a5568;  --border: #e2e8f0;   --bg: #f7fafc;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: var(--bg); color: #2d3748; font-size: 14px; }}
  header {{ background: #1a202c; color: #fff; padding: 1.5rem 2rem; }}
  header h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  header p {{ font-size: 13px; color: #a0aec0; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 1.5rem; }}
  .card {{ background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.25rem; }}
  .card .label {{ font-size: 12px; color: var(--gray); margin-bottom: 4px; }}
  .card .value {{ font-size: 26px; font-weight: 600; color: #1a202c; }}
  .card .value.green {{ color: var(--green); }}
  .card .value.red {{ color: var(--red); }}
  .notice {{ background: var(--blue-bg); border-left: 4px solid var(--blue); border-radius: 0 6px 6px 0;
             padding: 10px 14px; font-size: 13px; color: var(--blue); margin-bottom: 1.5rem; }}
  .tabs {{ display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 1rem; }}
  .tab {{ padding: 9px 18px; font-size: 13px; font-weight: 500; cursor: pointer;
           border-bottom: 2px solid transparent; margin-bottom: -2px; color: var(--gray); transition: all .15s; }}
  .tab.active {{ color: #1a202c; border-bottom-color: #1a202c; }}
  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}
  .search {{ width: 100%; padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px;
              font-size: 13px; margin-bottom: 10px; }}
  .table-wrap {{ border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: var(--bg); font-size: 12px; font-weight: 600; color: var(--gray);
        padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }}
  td {{ padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f7fafc; }}
  .badge {{ display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 500;
             padding: 3px 9px; border-radius: 99px; white-space: nowrap; }}
  .badge-add {{ background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }}
  .badge-del {{ background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }}
  .badge-in  {{ background: #e6fffa; color: #276749; font-size: 11px; padding: 2px 7px; border-radius: 99px; }}
  .rank {{ color: var(--blue); font-weight: 600; font-variant-numeric: tabular-nums; }}
  .code {{ font-family: monospace; font-size: 12px; background: var(--bg); padding: 2px 6px; border-radius: 4px; }}
  .footer {{ text-align: center; font-size: 12px; color: #a0aec0; padding: 2rem; margin-top: 1rem; }}
  @media (max-width: 600px) {{
    .grid {{ grid-template-columns: repeat(2, 1fr); }}
    th, td {{ padding: 6px 8px; font-size: 12px; }}
    .tab {{ padding: 8px 12px; font-size: 12px; }}
  }}
</style>
</head>
<body>
<header>
  <h1>📊 台股 0050 成份股異動分析</h1>
  <p>資料更新：{updated}　｜　資料來源：台灣證券交易所 opendata、元大投信</p>
</header>

<div class="container">

  <div class="notice">
    ⚠️ 分析規則（富時羅素指數編制邏輯）：市值排名<strong>進入前42名</strong>且未在0050 → 可能列入；
    市值排名<strong>落至57名之後</strong>且已在0050 → 可能踢除。每季（3/6/9/12月）正式審核一次。
  </div>

  <div class="grid">
    <div class="card"><div class="label">分析上市公司</div><div class="value">{s['top100_count']}</div></div>
    <div class="card"><div class="label">0050現有成份股</div><div class="value">{s['component_count']}</div></div>
    <div class="card"><div class="label">可能列入</div><div class="value green">{s['add_count']}</div></div>
    <div class="card"><div class="label">可能踢除</div><div class="value red">{s['del_count']}</div></div>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('changes')">📋 異動分析 ({s['add_count']+s['del_count']})</div>
    <div class="tab" onclick="switchTab('top100')">🏆 市值前{s['top100_count']}</div>
    <div class="tab" onclick="switchTab('components')">📦 現有成份股 ({s['component_count']})</div>
  </div>

  <!-- 異動分析 -->
  <div class="tab-pane active" id="pane-changes">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:110px">類型</th>
            <th style="width:70px">市值排名</th>
            <th style="width:75px">代號</th>
            <th>公司名稱</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>{changes_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- 市值前100 -->
  <div class="tab-pane" id="pane-top100">
    <input class="search" type="text" placeholder="搜尋代號或公司名稱…" oninput="filter('top100-body',this.value)">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:55px">排名</th>
            <th style="width:70px">代號</th>
            <th>公司名稱</th>
            <th style="width:95px;text-align:right">市值</th>
            <th style="width:80px;text-align:center">0050成份</th>
            <th style="width:80px;text-align:center">異動</th>
          </tr>
        </thead>
        <tbody id="top100-body">{top100_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- 現有成份股 -->
  <div class="tab-pane" id="pane-components">
    <input class="search" type="text" placeholder="搜尋代號或公司名稱…" oninput="filter('comp-body',this.value)">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:70px">代號</th>
            <th>公司名稱</th>
            <th style="width:75px">市值排名</th>
            <th style="width:95px;text-align:right">市值</th>
            <th style="width:80px;text-align:center">異動</th>
          </tr>
        </thead>
        <tbody id="comp-body">{comp_rows()}</tbody>
      </table>
    </div>
  </div>

</div>

<div class="footer">
  資料僅供參考，以富時羅素（FTSE Russell）正式公告為準 ｜
  <a href="https://www.twse.com.tw" target="_blank">TWSE</a>　
  <a href="https://www.yuantaetfs.com/product/detail/0050/ratio" target="_blank">元大投信0050</a>
</div>

<script>
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach((t,i) => {{
    t.classList.toggle('active', ['changes','top100','components'][i] === name);
  }});
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-' + name).classList.add('active');
}}
function filter(bodyId, q) {{
  const lq = q.toLowerCase();
  document.getElementById(bodyId).querySelectorAll('tr').forEach(r => {{
    r.style.display = r.textContent.toLowerCase().includes(lq) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""
    return html


# ── 主程式 ────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("台股 0050 成份股異動分析系統")
    print(f"執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    session = get_session()

    # 1. 抓市值排名
    top100 = fetch_market_cap_twse(session)
    if not top100:
        print("  TWSE opendata 失敗，嘗試備援資料源…")
        top100 = fetch_market_cap_pchome(session)
    if not top100:
        print("[ERROR] 無法取得市值資料，程式終止")
        raise SystemExit(1)

    time.sleep(1)

    # 2. 抓0050成份股
    components = fetch_0050_components(session)

    # 3. 分析
    result = analyze(top100, components)

    # 4. 輸出結果
    # out_dir = Path(__file__).parent.parent / "docs"
    out_dir = Path("docs")
    out_dir.mkdir(parents=True, exist_ok=True)
    

    # JSON（給後續程式用）
    json_path = out_dir / "result.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[輸出] JSON → {json_path}")

    # HTML 報告
    html = build_html(result)
    html_path = out_dir / "index.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[輸出] HTML → {html_path}")

    # 摘要
    print("\n" + "=" * 50)
    print("✅ 分析完成！")
    print(f"  可能列入 (Add)：{result['summary']['add_count']} 檔")
    for a in result["additions"]:
        print(f"    #{a['rank']:3d}  {a['code']} {a['name']}")
    print(f"  可能踢除 (Del)：{result['summary']['del_count']} 檔")
    for d in result["deletions"]:
        rank_str = str(d['rank']) if d['rank'] < 999 else "100+"
        print(f"    #{rank_str:>3}  {d['code']} {d['name']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
