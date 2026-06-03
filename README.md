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
| `QUARANTINE_CLEANUP_APPLY` | `false` | `true` = 隔離區安全清理真的丟垃圾桶；其餘 = 只報告(dry-run) |
| `QUARANTINE_CLEANUP_MIN_AGE_DAYS` | `30` | 只清進隔離早於 N 天的檔（額外安全緩衝） |
| `QUARANTINE_CLEANUP_AT_UTC` | `20:00` | 週日 20:00 UTC = 週一 04:00 TW |

> **quarantine_cleanup task**（2026-06 新增）：每週掃兩個書庫 root，找隔離資料夾
> (`_duplicates_quarantine`/`_archive_originals`/`_from_`/`_conflict`/`_manual_review`)
> 底下「md5+size+副檔名 三重與某個非隔離存活檔完全相同」的真重複，丟垃圾桶(30 天可救回)。
> 只碰 byte 完全相同的（同一份東西書庫別處還有），零誤刪風險。**預設 dry-run**；
> 確認首週 Telegram 報告無誤後，把 `QUARANTINE_CLEANUP_APPLY` 設 `true` 才真清。
> 本機等效腳本：`Library/scripts/quarantine_safe_cleanup.py`。
> Tier 2（同書不同格式/版本、byte 不同的）不在此 task 範圍，由人工/AI 定期審視。

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

> ⚠ **2026-06-03 已遷移 SA + relay 混合身分**（commit `10f4ff6`，根治每週 OAuth 重簽）。現行：讀走 Service Account `n8n-178@`（env `GOOGLE_SA_JSON`，base64；`USE_SA_CREDS=1`）、寫走 MarukoRestrictedRelay（env `RELAY_TOKEN`）。**下面的 OAuth 重簽流程已降為 rollback only**（設 `USE_SA_CREDS` 非 1 才會用到 user-OAuth 讀路徑）。完整紀錄與 Step 6 收尾待辦見 `docs/plans/2026-06-03-sa-relay-migration.plan.md`。Step 6（移除 user-OAuth 死碼、整段改寫本維護段）約 2026-06-10 後做。

- **[rollback only] OAuth refresh token 失效（約每 7 天，`invalid_grant: Token has been expired or revoked`）**：本服務的 Desktop OAuth client 掛在標準專案 `linecalendarbot-475101`，該專案 OAuth 同意畫面在 **Testing 模式** → refresh token 每 7 天到期被 revoke（**不是**「半年-1 年的安全撤銷」；背景見根倉 memory `gas_default_vs_standard_gcp_project_restricted_scope` 與計畫書 `claudeapi-gmail-restricted-scope-fix-2026-05-31.plan.md`）。收到 Telegram「merge_queue_poller 異常：invalid_grant」時的 3 步復原（**全程 CLI，不必開 Zeabur 面板**）：
  1. 本機重跑 `Library/scripts/auth_setup.py`（會開瀏覽器，**選主帳號 `cutegladys0708`** 同意，restricted 警告畫面按「進階→繼續」）→ 寫新 token 到 `憑證/library-cover-user-token.json`
  2. 重新 minify 副本（不必重 OAuth，只要 full json 還活著）：
     ```python
     import json
     i=json.load(open(r'E:\Dropbox\MarukoAutomation\憑證\library-cover-user-token.json',encoding='utf-8'))
     open(r'E:\Dropbox\MarukoAutomation\憑證\zeabur-handover\google_user_token.minified.txt','w',encoding='utf-8',newline='').write(json.dumps(i,separators=(',',':'),ensure_ascii=False))
     ```
  3. 推進 Zeabur 並重啟（service id `6a0dd12733d1a635fa380313`；`variable update -k` 對含逗號的長 JSON 實測可完整存入，2026-06-03 驗證）：
     ```bash
     VAL=$(cat 憑證/zeabur-handover/google_user_token.minified.txt)
     npx zeabur@latest variable update --id 6a0dd12733d1a635fa380313 -k "GOOGLE_USER_TOKEN_JSON=$VAL" -y -i=false
     npx zeabur@latest service restart --id 6a0dd12733d1a635fa380313 -y
     ```
     重啟後等下一次 5 分鐘 poll，log 出現 `[merge_queue_poller] queue status: ... (skip)` 無 `invalid_grant` 即復原。
  > ⚠ **不要為了省這週期把 `linecalendarbot-475101` 改成 Production**——會重新觸發 restricted Drive scope 的未驗證 runtime 牆（2026-05-31 relay 搬遷就是為了避開它）。Python/Zeabur 用 Desktop OAuth **無法**像 GAS 那樣靠免驗證預設專案規避（那是 GAS 專屬機制），走 relay 又會撞 30MB/6 分鐘上限，只能接受每週重簽 + Telegram 監看。
- **新書節奏改變**：若一週超過 200 本，調大 `MAX_ROWS_PER_RUN`，或改成週跑兩次
- **每週一固定看一次 Telegram 通知**：確認週末有跑

## 相關

- 主程式碼 / 一次性歷史修補腳本：`cutegladys/Library` 內 `scripts/stage2_process.py`、`auth_setup.py`
- 計畫書：`cutegladys/Library` 內 `docs/plans/library-cover-zeabur.plan.md`
- 整體流程脈絡：`MarukoWorkspace/交班_家裡與診所.md` Library 封面修補節
