# 台股 0050 成份股異動分析系統

自動抓取台灣上市公司市值排名與元大台灣50（0050）成份股，依照富時羅素指數編制規則，分析下次季度審核時可能列入或踢除的個股。

## 🌐 線上報告

部署後可在以下網址查看最新報告：
```
https://<你的GitHub帳號>.github.io/<Repository名稱>/
```

---

## 📊 分析規則

| 類型 | 條件 |
|------|------|
| **列入 (Add)** | 市值排名**前42名** 且 **尚未**在0050成份股中 |
| **踢除 (Delete)** | 市值排名**第57名之後** 且 **已在**0050成份股中 |

> 43～56名為緩衝區間，不觸發異動。每年 3、6、9、12 月正式審核並生效。

---

## 🗂️ 檔案結構

```
tw0050/
├── .github/
│   └── workflows/
│       └── analyze.yml      # GitHub Actions 排程設定
├── src/
│   └── analyze.py           # 主程式（爬蟲 + 分析 + 產生 HTML）
├── docs/
│   ├── index.html           # 輸出報告（GitHub Pages 自動發布）
│   └── result.json          # 原始分析結果（JSON格式）
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 🚀 部署步驟（一次性設定）

### 步驟一：建立 GitHub Repository

1. 登入 [github.com](https://github.com)
2. 點選右上角 **「+」→「New repository」**
3. Repository name：例如 `tw0050-analyzer`
4. 選 **Public**（GitHub Pages 免費方案需要 Public）
5. 點選 **「Create repository」**

### 步驟二：上傳程式碼

```bash
# 在本機執行
git clone https://github.com/<你的帳號>/tw0050-analyzer.git
cd tw0050-analyzer

# 複製本專案所有檔案進來，然後：
git add .
git commit -m "初始版本"
git push origin main
```

> 或直接在 GitHub 網頁介面用「Add file → Upload files」上傳。

### 步驟三：建立初始 docs/ 目錄

GitHub Actions 第一次執行前，需要先有 `docs/` 目錄：

```bash
mkdir docs
echo "<!-- placeholder -->" > docs/.gitkeep
git add docs/
git commit -m "建立 docs 目錄"
git push
```

### 步驟四：啟用 GitHub Pages

1. 進入 Repository → **Settings → Pages**
2. Source 選 **「Deploy from a branch」**
3. Branch 選 **「main」**，資料夾選 **「/docs」**
4. 點選 **Save**
5. 幾分鐘後網頁會在 `https://<帳號>.github.io/<repo名>` 上線

### 步驟五：手動觸發第一次執行

1. 進入 Repository → **Actions**
2. 點選左側 **「台股0050成份股異動分析」**
3. 點選右側 **「Run workflow」→「Run workflow」**
4. 等待約 1-2 分鐘，完成後即可查看報告

---

## ⚙️ 自訂設定

### 修改執行時間

編輯 `.github/workflows/analyze.yml` 中的 `cron`：

```yaml
# 現在設定：每個交易日 18:30 台灣時間（UTC 10:30）
- cron: '30 10 * * 1-5'

# 範例：每天早上 09:00 台灣時間（UTC 01:00）
- cron: '0 1 * * 1-5'
```

> Cron 時區為 UTC，台灣時間 = UTC+8

### 修改分析門檻

編輯 `src/analyze.py` 頂部常數：

```python
ADD_THRESHOLD = 42   # 前幾名可能列入
DEL_THRESHOLD = 57   # 幾名後可能踢除
TOP_N = 100          # 分析前幾大公司
```

---

## 💻 本機執行

```bash
# 安裝相依套件
pip install -r requirements.txt

# 執行分析
python src/analyze.py

# 開啟報告
open docs/index.html   # macOS
start docs/index.html  # Windows
```

---

## 📡 資料來源

| 資料 | 來源 | 說明 |
|------|------|------|
| 上市公司市值 | [TWSE opendata](https://opendata.twse.com.tw/) | 每日更新，排除ETF/上櫃 |
| 0050成份股 | 元大投信官網 → TWSE API → 備援名單 | 季度更新 |

---

## ⚠️ 免責聲明

本系統資料僅供參考，不構成投資建議。實際成份股異動以**富時羅素（FTSE Russell）** 正式公告為準。
