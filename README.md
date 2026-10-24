# 🎙️ 語音待辦秘書 × Google Calendar / Google Tasks 雙向同步 LINE 智慧助理
> **Voice Secretary & Google Calendar/Tasks Bi-Directional Sync Bot (`voice-calendar-sync-bot`)**

本專案結合了 **語音待辦秘書 (`VoiceSecretary`)** 的自然語音/口語意圖辨識與 **旅遊通勤助手 (`TravelWeatherBot`)** 的多日曆整合架構，全面升級為支援 **Google Calendar 日曆事件 (`CALENDAR_EVENT`)** 與 **Google Tasks 待辦任務 (`TASK`)** 自動分流、**雙向合併同步 (Push & Pull Two-Way Merge Sync)**、**智慧撞期衝突偵測与一鍵順延**、以及 **`.ics` / Google Calendar Deep Link 免憑證一鍵匯入** 的全功能 LINE 智慧秘書。

---

## ✨ 核心功能亮點

### 1. 🎙️ 語音 (M4A) 與自然語言雙引擎解析 (`ai_parser.py`)
- **多模態輸入**：同時支援 LINE 語音錄音訊息 (`audio/mp4`) 與口語文字輸入。
- **Gemini AI + 本地高精度 NLP 雙引擎 (Zero-Downtime Fallback)**：
  - 內建強大的繁體中文時間與語意正則解析引擎 (`parse_local_natural_language`)，即使未設定 `GEMINI_API_KEY` 或處於離線環境，也能 100% 精準解析「今天 / 明天 / 後天 / 大後天 / 本週X / 下週X / X月X日」、「早上/上午/中午/下午/晚上 X點(半)到Y點(半)」、「下班前」、地點（「地點在第一會議室」）與優先級（「高優先級 / 緊急 / 低優先級」）。
- **Event vs. Task 智慧自動分流**：
  - **📅 日曆事件 (`CALENDAR_EVENT`)**：具明確起訖時間、開會、季會、約訪或指定地點的行程（例如：「下週三下午兩點到四點跟中華資安團隊開季會，地點在第一會議室」），自動對應至 **Google Calendar**。
  - **✅ 待辦任務 (`TASK`)**：具截止期限（「...前要...」）或彈性採買/待辦事項（例如：「明天晚上八點前要交專案報告，高優先級」、「今天下班買牛奶跟雞蛋」），自動對應至 **Google Tasks**。

### 2. 🔄 Google Calendar & Google Tasks 雙向同步核心 (`google_sync_service.py` + `database.py`)
- **Bot ➔ Google (Push Sync)**：
  - 在 LINE 透過語音/文字新增，或點擊 Flex 卡片上的 Postback 按鈕「**✅ 完成**」、「**⏰ 延後 1 小時**」、「**⏩ 自動順延至下個空檔**」、「**🗑️ 刪除**」時，即時同步更新本地 SQLite 資料庫與遠端 Google Calendar / Google Tasks。
  - 支援 **Google REST API (OAuth2 Token)**、**Google Apps Script Webhook 橋接模式**，並在未設定雲端憑證時自動以 **Local Sync Twin 雙向鏡像模式** 運作，完整記錄 `google_event_id`、`google_task_id`、`etag`、`sync_status` 與 `last_synced_at`。
- **Google ➔ Bot (Pull & Two-Way Merge Sync)**：
  - 支援從遠端 Google Calendar (REST API + 多組 iCal 訂閱 URLs) 與 Google Tasks 拉取新增或修改的行程與待辦。
  - 透過 `etag` 與 `updated_at` 進行 **雙向合併 (Two-Way Merge)**：自動識別遠端新增、遠端修改覆寫（標記 `CONFLICT_RESOLVED`）或本地較新反向推回遠端。

### 3. ⚠️ 智慧撞期偵測與 `.ics` / Google Deep Link 一鍵匯出 (`ics_generator.py`)
- **智慧撞期偵測 (Conflict Detection)**：
  - 新增或順延日曆事件時，自動檢查該時段是否與現有未完成會議重疊。
  - 若偵測到撞期，於 LINE Flex Message 卡片中顯示醒目的紅框 **「⚠️ 時段衝突警示」**，並提供 **「⏩ 自動順延至下個空檔」** Postback 按鈕（透過 `find_next_available_slot` 自動計算最近無衝突時段並同步更新 Google Calendar）。
- **免 API 憑證也能一鍵寫入手機行事曆**：
  - 每張 Flex 卡片底部皆內建 **「📅 加入 Google 日曆 (Deep Link URL)」** 與 **「📥 下載 .ics 檔 (`/api/ics/{item_id}.ics`)」**（符合 RFC 5545 標準並內含 30 分鐘前 `VALARM` 提醒），一鍵寫入 iPhone (iOS Calendar)、Android、Outlook 或 Apple Calendar。

### 4. ⏰ 排程主動推播與晨間雙向日程總覽 (`main.py` + `flex_templates.py` + `setup_rich_menu.py`)
- `POST /api/cron/morning-brief`：每日晨間自動觸發 Google 雙向同步，將 **今日 Google Calendar 行程 + 未完成 Google Tasks** 彙整成時間軸 Flex 卡片主動推播。
- `POST /api/cron/upcoming-reminders`：定時掃描未來 30 分鐘內即將開始的會議或即將到期的任務，主動推播提醒卡片。
- `setup_rich_menu.py`：自動繪製並綁定三宮格 LINE 圖文選單（`☀️ 今日日程` / `🔄 雙向同步` / `📋 全部清單`）。

---

## 🗂️ 專案架構

```text
voice-calendar-sync-bot/
├── api/
│   └── index.py                  # Vercel Serverless 入口
├── ai_parser.py                  # Gemini 多模態語音轉譯 + 本地高精度中文時間/語意解析引擎
├── config.py                     # 環境變數與預設參數管理
├── database.py                   # SQLAlchemy 模型 (ScheduleItem)、撞期偵測與空檔搜尋演算法
├── flex_templates.py             # LINE Flex Message 卡片模板 (含衝突警示、Postback 與 Deep Link)
├── google_sync_service.py        # Google Calendar & Tasks 雙向同步核心 (Push + Pull Two-Way Merge)
├── ics_generator.py              # RFC 5545 .ics 行事曆生成器與 Google Calendar Deep Link 產生器
├── main.py                       # FastAPI 主程式、LINE Webhook、Postback 路由與 Cron 排程端點
├── setup_rich_menu.py            # LINE 圖文選單 (Rich Menu) 自動繪圖與部署腳本
├── test_voice_calendar_sync.py   # 完整單元與端點整合測試套件 (Pytest)
├── requirements.txt              # Python 依賴套件清單
├── Dockerfile                    # 容器化部署設定
├── render.yaml                   # Render 雲端部署設定
├── vercel.json                   # Vercel Serverless 部署設定
└── .env.example                  # 環境變數範例檔
```

---

## 🚀 快速啟動與測試

### 1. 安裝依賴與環境設定
```bash
cp .env.example .env
pip install -r requirements.txt
```

### 2. 執行單元與整合測試
```bash
pytest -v test_voice_calendar_sync.py
```

### 3. 啟動本地開發伺服器
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 📡 API 端點一覽

| 方法 | 路徑 | 說明 |
| :--- | :--- | :--- |
| `GET` | `/` , `/api` | 服務健康檢查與當前同步模式 (`sync_mode`) 狀態 |
| `POST` | `/webhook` , `/api/webhook` | LINE Messaging API Webhook（支援語音 `audio`、文字 `text` 與 `postback` 互動） |
| `GET` | `/api/ics/{item_id}.ics` | 下載指定行程或任務之標準 RFC 5545 `.ics` 行事曆檔 |
| `POST` | `/api/sync/google` | 觸發 Google Calendar & Tasks 雙向合併同步 (Two-Way Merge) |
| `POST` | `/api/cron/morning-brief` | 觸發每日晨間日程總覽 (Calendar + Tasks 時間軸卡片) |
| `POST` | `/api/cron/upcoming-reminders` | 觸發未來 30 分鐘內會議/任務到期主動提醒 |
