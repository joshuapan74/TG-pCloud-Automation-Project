# TG Auto Downloader — Windows / macOS 雙平台通用版

這是一個具備 Apple 設計風格介面、系統匣常駐（System Tray / Menu Bar）與圖形化設定精靈的自動化工具。當您指定的 Telegram 對話框收到檔案、照片或壓縮檔時，程式會在背景自動下載（≥ 50 MB 自動啟用 4 路並行分段引擎），處理完成後搬移至您指定的本機雲端同步資料夾（例如 **pCloud Drive**、Google Drive、OneDrive 等），並交由雲端硬碟桌面程式負責上傳。

---

## ✨ 功能特色

1. **圖形化帳號設定與登入（免開終端機）**
   - 支援在介面輸入個人 Telegram `API ID`、`API Hash`、手機號碼、驗證碼與兩步驟驗證密碼（2FA），支援「登出／切換帳號」。
2. **自由指定抓取的 Telegram 對話框**
   - 預設監聽 **「我的收藏（Saved Messages）」**，也可在設定頁點擊 **「從 Telegram 選擇對話框...」**，搜尋並指定任何私人對話、群組或頻道。
3. **重啟斷點續傳與孤兒暫存清理**
   - 尚未完成的任務會持久化保存；重開程式連上 Telegram 後自動取回任務並接續 `.parts/` 斷點續傳。
   - 下載暫存區（預設 `~/Downloads/TelegramAuto/_temp`）與成品資料夾（預設 `~/Downloads/TelegramAuto`）完全分離；啟動時自動清理不屬於任何保留任務的孤兒暫存檔。
   - 可選擇性開啟 **「補抓程式關閉期間的新檔案」**（預設關閉）。
4. **自訂上傳同步資料夾（不經手您的 pCloud 帳密）**
   - 直接透過資料夾選擇視窗指定目標同步資料夾；若原本設定的磁碟機或同步資料夾找不到，系統會自動彈出強制視窗引導重選（預設 `~/Downloads/TelegramAuto`）。
5. **IDM 級 4 路並行下載 + ZIP 自動解壓與加密隔離**
   - ≥ 50 MB 自動啟用 4 路並行分段下載與 60 秒看門狗；遇到加密 ZIP 會自動隔離並顯示警告、不刪除雲端訊息。
   - 在監聽的對話框傳送 `/回報` 可遠端查詢目前下載進度與剩餘空間。

---

## 🛠️ 直接以 Python 執行（開發／測試模式）

### Windows

雙擊 [run_test.bat](file:///d:/githubforfun/TG-pCloud-Automation-Project/tg-auto-downloader/run_test.bat)，或在終端機執行：
```bat
cd tg-auto-downloader
venv\Scripts\python.exe main.py
```

### macOS

```bash
cd tg-auto-downloader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

---

## 📦 打包成獨立執行檔

打包產出物**不包含**任何個人憑證或 Session，可直接分發給其他使用者：

- **Windows 打包成 `.exe`**：執行 [build_windows.bat](file:///d:/githubforfun/TG-pCloud-Automation-Project/tg-auto-downloader/build_windows.bat)，產出 `dist\TG Auto Downloader.exe`。
- **macOS 打包成 `.app`**：執行 `chmod +x build_app.sh && ./build_app.sh`，產出 `dist/TG Auto Downloader.app`。
