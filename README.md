# TG-pCloud 自動備份助手（TG Auto Downloader）

把 Telegram 裡的影片、大檔案、照片與壓縮檔，**自動下載、解壓並歸檔到你的雲端同步資料夾**（如 **pCloud Drive**、Google Drive、OneDrive 或任意本機／NAS 資料夾），支援最高 **2GB 單檔** 與 **4 路並行加速下載**，提供 **Windows 與 macOS** 雙平台系統匣常駐程式與圖形化設定介面。

> 🔒 **隱私設計原則**：本工具完全在本機運作，**不經手、不儲存任何雲端硬碟帳密**。你的 Telegram 憑證與登入授權只保存在自己電腦的使用者設定目錄中，不會回傳任何第三方伺服器。

---

## ✨ 為什麼需要這個工具？

Telegram 一般 Bot API 有 **20MB 檔案大小限制**，遇到大型影片或壓縮檔便無法遠端備份。  
本專案透過個人帳號的 MTProto 協議（`Telethon`）突破 20MB 限制，並搭配電腦上的雲端同步桌面軟體（例如 **pCloud Drive**），形成一條全自動流水線：

```text
Telegram 指定對話框收到檔案（我的收藏 / 群組 / 頻道 / 私人對話）
        ↓
自動偵測並排隊下載（≥ 50 MB 啟動 4 路並行分段引擎 + 斷點續傳）
        ↓
暫存於獨立工作區（_temp），若為標準 ZIP 則自動解壓（加密 ZIP 自動隔離警告）
        ↓
搬移至你指定的同步資料夾（例如 pCloud Drive 同步目錄）並二次大小驗證
        ↓
確認 100% 寫入成功後，由雲端同步軟體自動上傳，並依設定選擇是否刪除 Telegram 原檔
```

---

## 🚀 核心功能一覽

- **全圖形化設定精靈（免碰終端機）**：直接在介面輸入 Telegram `API ID`、`API Hash`、手機號碼、驗證碼與兩步驟密碼（2FA），支援一鍵登出與切換帳號。
- **自由指定監聽的 Telegram 對話框**：
  - 預設監聽 **「我的收藏（Saved Messages）」**。
  - 可**直接貼上** `@用戶名`、`https://t.me/...`、私人群組訊息連結（`https://t.me/c/.../123`）或數字 Chat ID，也能從 Telegram 對話清單搜尋選取。
- **IDM 級 4 路並行下載 + 斷點續傳**：
  - `< 50 MB`（或照片）：單一高速串流（512 KB chunk）。
  - `≥ 50 MB`：自動啟用 **4 路並行分段下載**，搭配 60 秒無回應看門狗與重試機制。
- **重啟任務保留與暫存分離**：
  - 未完成或中斷的任務會自動保存；重開程式連上 Telegram 後自動接續 `.parts` 斷點下載。
  - 下載暫存工作區（預設 `~/Downloads/TelegramAuto/_temp`）與成品資料夾（預設 `~/Downloads/TelegramAuto`）完全分離，啟動時自動清理孤兒暫存檔。
  - 可選開啟 **「補抓程式關閉期間的新檔案」**（預設關閉）。
- **防呆機制**：
  - **加密 ZIP 隔離**：偵測到加密壓縮檔立即停止搬移、保留在暫存區並跳出警告，不會刪除 Telegram 原檔。
  - **磁碟機遺失保護**：若指定的雲端虛擬磁碟未掛載或找不到，會自動彈出置頂視窗引導重選目標資料夾。
- **遠端查詢與系統匣常駐**：
  - 在監聽的對話框傳送 `/回報`，即刻回傳目前下載進度、速度、ETA 與磁碟剩餘空間。
  - 支援 Windows 登錄檔與 macOS LaunchAgent 開機自動啟動，並提供一鍵清除佇列與歷史紀錄。

---

## 💻 快速開始（一般使用者）

### 方式 A：Windows 直接使用 `.exe`（免安裝 Python）

1. 前往本專案的 **Releases** 頁面下載 `TG Auto Downloader.exe`（或自行依下方「自行打包」步驟產生）。
2. 雙擊執行 `TG Auto Downloader.exe`：
   - 第一次啟動會自動跳出控制面板（同時右下角系統匣會出現常駐圖示）。
3. **完成 2 步驟設定即可開始使用**：
   - **步驟 1（帳號登入）**：前往 [my.telegram.org](https://my.telegram.org) 登入後點選 `API development tools` 建立一組個人 `App api_id` 與 `App api_hash`，填入程式分頁 1，輸入手機號碼與驗證碼完成登入。
   - **步驟 2（指定對話框與目標資料夾）**：切到分頁 2，選擇你要監聽的 Telegram 對話框（預設為「我的收藏」），並選擇你的 **pCloud Drive**（或其他雲端硬碟）本機同步資料夾，點選 **「儲存設定並套用」**。

> 💡 如果沒有安裝任何雲端同步軟體，也可以直接使用預設資料夾 `~/Downloads/TelegramAuto` 當作純本機自動下載歸檔工具。

---

### 方式 B：從原始碼執行（Windows / macOS 通用）

#### Windows

```bat
git clone <你的倉庫網址>
cd TG-pCloud-Automation-Project\tg-auto-downloader
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

#### macOS

```bash
git clone <你的倉庫網址>
cd TG-pCloud-Automation-Project/tg-auto-downloader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

---

## 📦 自行打包成獨立執行檔

打包腳本只會打包程式碼與介面圖示，**不會**包含任何個人設定、`.env` 或登入 Session。

- **Windows（產出 `dist\TG Auto Downloader.exe`）**：
  ```bat
  cd tg-auto-downloader
  build_windows.bat
  ```
- **macOS（產出 `dist/TG Auto Downloader.app`）**：
  ```bash
  cd tg-auto-downloader
  chmod +x build_app.sh
  ./build_app.sh
  ```

---

## 📁 本機設定與專案結構

### 使用者個人資料儲存位置（不會存進專案目錄）
- **Windows**：`%APPDATA%\TGAutoDownloader\`
- **macOS**：`~/Library/Application Support/TGAutoDownloader/`

內含你的 `config.json`、`auto_downloader_session.session`、`pending_queue.json` 與 `downloader.log`。若要徹底清除登入紀錄，可在介面點選「登出目前帳號」或直接刪除上述資料夾。

### 專案目錄一覽

```text
TG-pCloud-Automation-Project/
├── README.md                    # 本說明文件
├── .gitignore                   # 忽略敏感憑證、Session 與建置產物
└── tg-auto-downloader/          # Windows / macOS 雙平台通用桌面應用程式
    ├── main.py                  # 主程式入口（系統匣常駐 + 任務排程 + 子進程 GUI）
    ├── gui_app.py               # Apple 風格控制面板、對話框選擇與強制路徑提示
    ├── tg_service.py            # Telegram MTProto 連線、4 路並行引擎與連結解析
    ├── file_service.py          # 個人設定、暫存分離、孤兒清理與搬移驗證
    ├── queue_service.py         # 下載佇列持久化與歷史紀錄管理
    ├── platform_utils.py        # 跨平台路徑、開機啟動與 Squircle 圖示生成
    ├── utils.py                 # 大小格式化與進度條工具
    ├── assets/                  # App 視窗與執行檔圖示 (app_icon.ico / app_icon.png)
    ├── requirements.txt         # Python 相依套件清單
    ├── build_windows.bat        # Windows PyInstaller 打包腳本
    ├── build_app.sh             # macOS PyInstaller 打包腳本
    └── setup.py                 # macOS py2app 打包設定（備用）
```

---

## ❓ 常見問題

1. **為什麼 Windows 必須在 Windows 電腦打包、macOS 必須在 Mac 打包？**
   - `PyInstaller` 與 `py2app` 皆不支援跨作業系統編譯二進位執行檔。如果你沒有 Mac，也可以直接用 macOS 的 Python 執行 `python3 main.py`，或透過 GitHub Actions（`macos-latest`）自動建置 `.app`。
2. **監聽別人的頻道或群組時，檔案會把對方訊息刪掉嗎？**
   - 只有在你對該對話框擁有刪除權限（例如「我的收藏」或你自己管理的群組／頻道）且有勾選「確認移轉成功後自動刪除原檔訊息」時才會刪除；若無權限，程式會自動保留訊息並標記為 `已完成移轉(無刪除權限)`，不會報錯。
3. **一次傳很多檔案會同時下載嗎？**
   - 檔案之間採「依序排隊」處理，單一大檔（≥ 50 MB）內部才會啟用 4 路並行連線，以兼顧下載速度並避免觸發 Telegram 速率限制（`FloodWait`）。
