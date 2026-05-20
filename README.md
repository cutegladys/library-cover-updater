# library-cover-updater

Library 試算表新書封面自動補圖（Zeabur 排程，獨立容器）。

每週日 02:00 TW（週六 18:00 UTC）掃 Library 試算表新進來的書（U 欄有 Drive fileId 且 Q 欄非 Drive 圖），依檔型分流：

- **PDF** → PyMuPDF 渲染首頁 2x PNG → 上傳到 Cover Art 資料夾 → 寫 IMAGE 公式
- **EPUB** → 解 zip 抓內建封面 → 同上
- **image** → `uc?export=view` 直連
- **audio / video / 其他** → Drive thumbnailLink fallback

寫完同時把 F 欄 Source 改成 `"Google Drive"`，雙重保險避免未來被網路圖覆蓋。

## 為什麼獨立容器

設計上是 zeabur-task-runner 的姐妹 service，但**刻意分開**避免：

- PyMuPDF 依賴衝突拖垮班表同步 / 配發比對等核心 task
- PDF 渲染瞬間吃 RAM 引發 OOM 影響其他 task
- 部署失敗連帶其他 task

獨立容器互不影響。

## 環境變數

### 必填

| Key | 說明 |
|---|---|
| `GOOGLE_USER_TOKEN_JSON` | 主帳號 OAuth refresh token 整包 minified JSON。本機跑過 `Library/scripts/auth_setup.py` 後拿 `憑證/library-cover-user-token.json` 整檔 minify 成一行 |
| `LIBRARY_SHEET_ID` | `1lb3M0z-aN0B1NpdPlQSFOPpoT94_Yo-ipsswDmAVuaQ` |
| `COVER_ART_FOLDER_ID` | `1jSDRlKWWrQrYHw1XzOfTsJ1AIS7lgTbk` |
| `TELEGRAM_BOT_TOKEN` | Bot Token（建議用既有 MainBot 或 SecretaryBot 的 token） |
| `TELEGRAM_CHAT_ID` | 你的 chat id |

### 可選

| Key | 預設 | 說明 |
|---|---|---|
| `MAX_ROWS_PER_RUN` | `200` | 單次最多處理筆數（避免逾時） |
| `LIBRARY_COVER_UPDATER_AT_UTC` | `18:00` | 週六 18:00 UTC = 週日 02:00 TW |
| `COVER_UPDATER_DRY_RUN` | `0` | 1 = 只印不寫（除錯） |
| `RUN_ON_START` | `false` | 1 = 容器啟動立即跑一次（部署驗證用） |

## Zeabur 部署 SOP

1. 在 Zeabur 建新 service → 從 GitHub repo `cutegladys/library-cover-updater`、main branch
2. **Health Check 關閉**（此 service 不監聽任何 port）
3. Dockerfile 自動偵測（repo 根目錄有 `Dockerfile`）
4. Variables 設上述環境變數（必填 5 個）
5. Deploy → 看 Logs：
   ```
   Library Cover Updater 啟動
     排程：Every saturday at 18:00:00 do job() ...
   ```
6. **第一次驗證**（建議）：暫時設 `RUN_ON_START=true` + `COVER_UPDATER_DRY_RUN=1`，Redeploy → 看 logs 跑出「ℹ️ 本次無新書」或統計，確認流程通。確認後**移除這兩個變數**。

## 維護

- **OAuth refresh token 失效**：Google 偶因安全偵測撤銷（半年-1 年一次）。收到 Telegram「❌ Library 封面修補失敗：invalid_grant」→ 本機重跑 `Library/scripts/auth_setup.py` → 更新 Zeabur 的 `GOOGLE_USER_TOKEN_JSON`
- **新書節奏改變**：若一週超過 200 本，調大 `MAX_ROWS_PER_RUN`，或改成週跑兩次
- **每週一固定看一次 Telegram 通知**：確認週末有跑

## 相關

- 主程式碼 / 一次性歷史修補腳本：`cutegladys/Library` 內 `scripts/stage2_process.py`、`auth_setup.py`
- 計畫書：`cutegladys/Library` 內 `docs/plans/library-cover-zeabur.plan.md`
- 整體流程脈絡：`MarukoWorkspace/交班_家裡與診所.md` Library 封面修補節
