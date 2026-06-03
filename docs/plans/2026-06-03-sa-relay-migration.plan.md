---
title: library-cover-updater 改 SA + relay 混合身分（根治每週重簽）
type: 改善/重構
status: 待執行
created: 2026-06-03
owner: Gladys
todos:
  - "[relay] drive.js 新增 driveUpload(base64→建檔到指定夾+設公開讀→回 fileId)"
  - "[relay] drive.js 新增 driveMove(fileId+addParent+removeParent→搬檔)"
  - "[relay] Code.js switch 加 case 'drive.upload' / 'drive.move'；RELAY_VERSION bump；clasp push + deploy -i；GET ?action=health 確認新版"
  - "[lcu] utils/relay.py 新增：POST helper(RELAY_URL+RELAY_TOKEN, action, params)"
  - "[lcu] utils/oauth.py：load_user_creds 改 load_sa_creds(讀 env GOOGLE_SA_JSON)"
  - "[lcu] utils/drive.py：upload_cover 改走 relay drive.upload；get_metadata/download_media 維持 SA 直連"
  - "[lcu] inbox_processor.py：files().create(封面)改 relay upload；move_file_to_folder 改 relay drive.move"
  - "[lcu] duplicate_merger.py：搬隔離改 relay drive.move（確認實際呼叫點）"
  - "[lcu] quarantine_cleanup.py：files().update(trashed)改 relay drive.trash"
  - "[lcu] 全 task build() 改用 SA creds；env 增 GOOGLE_SA_JSON/RELAY_URL/RELAY_TOKEN"
  - "[user 手動] 把 Library sheet 分享給 SA(編輯者)；書源夾+Cover Art 夾+inbox/review/隔離夾分享給 SA(檢視者)"
  - "[驗證] 先 DRY_RUN+RUN_ON_START 跑一輪、看 log 各 task 通；保留舊 user-OAuth 路徑一週當 rollback"
  - "[收尾] 確認穩定後移除 user-OAuth 死碼 + GOOGLE_USER_TOKEN_JSON env；更新 README 維護段；改 memory/CLAUDE.md 索引"
---

# library-cover-updater 改 SA + relay 混合身分

## 你要做的事（一句話）

把這支 Zeabur Python 服務的 Google 身分，從「會每 7 天過期、要手動瀏覽器重簽的使用者 OAuth」換成「**Service Account（讀的部分，永不過期）+ MarukoRestrictedRelay（建檔/搬檔/丟垃圾桶這幾個擁有者動作，以你身分做）**」的混合，徹底消除每週重簽，同時**保留 PyMuPDF 與 Zeabur 無 timeout 的優勢**。

## 背景和動機

- 這支用「使用者 OAuth」、OAuth client 掛在標準專案 `linecalendarbot-475101`、該專案同意畫面在 **Testing 模式** → refresh token **每 7 天**被 Google revoke（背景見根倉 memory `project_library_cover_updater_token_weekly_testing_expiry` 與 `gas_default_vs_standard_gcp_project_restricted_scope`、計畫書 `claudeapi-gmail-restricted-scope-fix-2026-05-31.plan.md`）。
- **不能改 Production**（會重新撞 restricted Drive 未驗證牆，正是 5/31 relay 搬遷要逃離的）。
- **不能全搬回 GAS**：PyMuPDF 渲染 PDF 封面「GAS 做不到」、picture_books 會撞 6 分鐘超時（`main.py` 自己的註解，正是當初離開 GAS 的原因）。
- **SA 為什麼不能全包**：個人 Gmail 的 SA 沒有 My Drive 配額、也不能丟別人的檔 → 「建檔/搬檔/丟垃圾桶」這三類擁有者動作 SA 做不到，必須走以使用者身分執行的 relay。

## 目標架構

| 動作類別 | 由誰做 | 細節 |
|---|---|---|
| 讀 metadata、下載書檔(500MB)、列資料夾、sheet 讀寫 | **Service Account** | token 永不過期；資料夾/sheet 需事先分享給 SA |
| 建封面檔(`upload_cover`)、PDF 封面建檔 | **relay `drive.upload`**(新增) | base64 小圖、不撞 30MB/6 分鐘 |
| 搬書檔(`move_file_to_folder` / 重複移隔離) | **relay `drive.move`**(新增) | 只傳 fileId+父夾 |
| 丟垃圾桶(`quarantine_cleanup`) | **relay `drive.trash`**(已存在) | 只傳 fileId |

封面**仍存在你的 Drive Cover Art 夾、位置不變**（relay 以你身分建檔，擁有者是你，與既有封面一致；不改外部網址）。

## 具體步驟

### Step 1：relay 端新增 drive.upload / drive.move
- 做什麼：`MarukoRestrictedRelay/drive.js` 新增兩個函式：
  - `driveUpload({folderId, name, mimeType, contentBase64})` → `DriveApp.getFolderById(folderId).createFile(Utilities.newBlob(Utilities.base64Decode(...), mimeType, name))` → `setSharing(ANYONE_WITH_LINK, VIEW)` → 回 `{fileId}`。
  - `driveMove({fileId, addParent, removeParent})` → DriveApp 取檔、`addParent.addFile / removeParent.removeFile`（或 Drive Advanced `Files.update` addParents/removeParents）→ 回 `{ok}`。
  - `Code.js` switch 加 `case 'drive.upload'` / `case 'drive.move'`；header 註解 action 清單補上；`RELAY_VERSION` bump。
- 產出：relay 多兩個能以使用者身分建檔/搬檔的 endpoint。
- 注意：relay 是**固定 URL web app** → 改完 `clasp push` + `clasp deploy -i <relay deploymentId>`（走 `/gas-deploy`），`GET ?action=health` 確認 version 是新的、scopes 含 drive。scope 已是 `drive` full，**不必改 manifest / 不必重新授權**。

### Step 2：library-cover-updater 新增 relay helper + 換身分
- 做什麼：
  - 新 `utils/relay.py`：`relay_call(action, params)` POST `RELAY_URL`，body `{token: RELAY_TOKEN, clientName:"library-cover-updater", action, params}`，回 `result`／raise on error。
  - `utils/oauth.py`：`load_user_creds()` → `load_sa_creds()`，讀 env `GOOGLE_SA_JSON`（SA 金鑰整包），`service_account.Credentials.from_service_account_info(info, scopes=[drive, spreadsheets])`。
- 產出：讀的路徑全改 SA，寫的路徑有 relay 管道。
- 注意：SA scope 用 `drive`(讀/列/下載) + `spreadsheets`。

### Step 3：把三類「寫」操作改打 relay
- 做什麼：
  - `utils/drive.py upload_cover()` → 改呼叫 `relay_call("drive.upload", {...})` 回 fileId（介面對呼叫端不變）。
  - `inbox_processor.py`：line 77 的 `files().create`(封面) 改 relay upload；`move_file_to_folder` 與所有 `files().update(addParents/removeParents)` 改 `relay_call("drive.move", ...)`。
  - `duplicate_merger.py`：找出搬隔離的呼叫點，改 relay drive.move（**實作前先讀全檔確認**）。
  - `quarantine_cleanup.py`：line 143 `files().update(trashed:True)` 改 `relay_call("drive.trash", {fileId})`。
- 產出：所有擁有者動作改以使用者身分（relay）執行；其餘 build() 用 SA。
- 注意：把散落在 task 內的 inline drive 寫操作收斂進 `utils/drive.py` 統一走 relay，避免漏改。

### Step 4：分享資源給 SA（**只有你能做的手動步驟**）
- 做什麼：把以下分享給 SA email（Step 0 先確認用哪顆，預設 `n8n-178@linecalendarbot-475101.iam.gserviceaccount.com`）：
  - Library sheet `1lb3M0z-aN0B1NpdPlQSFOPpoT94_Yo-ipsswDmAVuaQ` → **編輯者**
  - Cover Art 夾 `1jSDRlKWWrQrYHw1XzOfTsJ1AIS7lgTbk` → 檢視者
  - 書源根夾（Ebook root、繪本 root）、inbox/review/隔離父夾 → 檢視者（實作時我列出完整 folderId 清單給你逐一分享）
- 產出：SA 讀得到所有它要讀的東西。
- 注意：分享屬「prohibited action」AI 不能代做（對應 memory `zeabur_migration_sa_sheet_share`）；我會給你「貼上 SA email + 該分享清單」。

### Step 5：測試驗證（不碰 production 排程）
- 做什麼：在 Zeabur 暫設 `GOOGLE_SA_JSON`+`RELAY_URL`+`RELAY_TOKEN`、`COVER_UPDATER_DRY_RUN=1`+`RUN_ON_START=true`、Redeploy → 看 log 各 task（cover_drive/inbox_processor/folder_sync/drive_index/picture_books/duplicate_*/quarantine_cleanup/merge_queue_poller）都跑得過、無 invalid_grant、relay 呼叫成功。確認後關 DRY_RUN/RUN_ON_START。
- 產出：確認新身分整條鏈路通。
- 注意：保留 `load_user_creds` 死碼 + `GOOGLE_USER_TOKEN_JSON` env **一週**當 rollback；真出事先切回舊路徑再修。

### Step 6：收尾
- 做什麼：穩定一週後移除 user-OAuth 死碼 + `GOOGLE_USER_TOKEN_JSON`；更新 `README.md` 維護段（OAuth 重簽流程整段拔掉，改寫成 SA+relay 架構＋「SA 金鑰怎麼換」）；更新根倉 memory `project_library_cover_updater_token_weekly_testing_expiry`（標記已根治）。
- 產出：乾淨的單一身分模型、文件對齊。

## 預計成果

- **永遠不必再每週手動瀏覽器重簽**（SA token 不過期；relay 在免驗證預設專案也不過期）。
- 保留 PyMuPDF 高解析 PDF 封面 + Zeabur 無 timeout。
- 封面仍在你的 Drive、與既有資料一致。
- relay 多了 upload/move 兩個 action，未來其他 Python/Zeabur 服務要對個人 Drive 建檔/搬檔也能複用。

## 不包含在這次的範圍

- 不改封面儲存位置（不上 R2/物件儲存）。
- 不動標準專案 `linecalendarbot-475101` 的發布狀態（維持 Testing）。
- 不重寫 task 業務邏輯（只換身分管道）。
- 不碰主帳號 GAS 那 3 個 Library trigger。

## 可能遇到的風險

- **relay drive.move 對「非擁有者父夾」reparent 失敗**：relay 以使用者身分跑、檔又是使用者擁有，理論上沒問題；萬一某次 addParents/removeParents 報錯，fallback 用 Advanced Drive `Files.update`。
- **SA 列不到某些書檔**：因資料夾沒分享到 → Step 4 清單要完整；drive_index 跑出來「少一批」時先查資料夾分享。
- **relay 呼叫量**：inbox_processor/merger 搬檔變多次 HTTP（小 payload），注意別撞 GAS 每日 UrlFetch 配額；量小（週跑、各數十筆）應無虞，但收尾時看 RelayLog 確認。
- **SA 金鑰外洩風險**：`GOOGLE_SA_JSON` 只放 Zeabur Variables，不入 git（`.env` 已 gitignore）。
