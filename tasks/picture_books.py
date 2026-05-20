"""
英文繪本資源 Inbox 處理器 — 簡化版（Phase 4-1）

對應 pictureBooksProcessor.js (553 行) + driveIndex.js rebuildDriveIndex / linkDriveFilesFromNote。

GAS 原版因 6 分鐘 timeout、用 sentinel + 自我排程分段續跑、很複雜。
Python 容器無 6 分鐘限制 → recursive list 一次跑完，不必 sentinel/queue。

流程：
1. recursive list root folder（英文讀本資源 1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx）下所有檔
2. 收集 fileId set；對比 ALL 分頁 V 欄已知 fileId 找 orphans
3. 寫 _PB_Draft 分頁：file_id / file_name / mime / folder_path（給 user review）
4. Telegram 通知 N 個 orphan

⚠️ 簡化點（vs GAS 原版）：
- 不做 sentinel 偵測（原版要等 Python inbox_watcher.py 完成才觸發）
- 不做 Gemini 輔助書目草稿（GAS 原版 _pbWriteDraft 用 Gemini parse epub）
  → 只列 orphan、寫進 _PB_Draft 給 user 看，不自動寫進 ALL
- 不做檔名 normalize（draft 就是原始檔名）

env:
  COVER_UPDATER_DRY_RUN  1 = 只列、不寫 sheet
"""
import logging
import os
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.picture_books")

PICTUREBOOKS_ROOT_ID = "1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx"
MAIN_SHEET = "ALL"
DRAFT_SHEET = "_PB_Draft"

# 1-based columns in ALL
COL_FILE_ID_ALL = 22  # V


def list_files_recursive(drive_service, root_id: str):
    """遞迴列 root 下所有非 folder 檔案，回 list of dict {id, name, mimeType, path}。"""
    result = []
    # path stack: [(folder_id, "/parent/path", folder_name)]
    stack = [(root_id, "", "")]
    visited = set()

    while stack:
        folder_id, parent_path, folder_name = stack.pop()
        if folder_id in visited:
            continue
        visited.add(folder_id)

        current_path = parent_path + ("/" + folder_name if folder_name else "")

        page_token = None
        while True:
            try:
                r = drive_service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
            except HttpError as e:
                logger.warning(f"  list err in {folder_id}: {e}")
                break

            for f in r.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    stack.append((f["id"], current_path, f["name"]))
                else:
                    result.append({
                        "id": f["id"],
                        "name": f["name"],
                        "mime": f["mimeType"],
                        "path": current_path,
                    })

            page_token = r.get("nextPageToken")
            if not page_token:
                break

    return result


def ensure_draft_sheet(sheets_service, spreadsheet_id, dry_run):
    """確保 _PB_Draft 分頁存在。"""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == DRAFT_SHEET:
            return
    if dry_run:
        logger.info(f"[picture_books] (dry_run) would create sheet '{DRAFT_SHEET}'")
        return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{"addSheet": {"properties": {"title": DRAFT_SHEET}}}],
        },
    ).execute()


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    creds = load_user_creds()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    # 1. recursive list root
    logger.info(f"[picture_books] 開始 recursive list {PICTUREBOOKS_ROOT_ID}...")
    start = time.time()
    files = list_files_recursive(drive_service, PICTUREBOOKS_ROOT_ID)
    logger.info(f"[picture_books] 共找到 {len(files)} 個檔案 ({time.time()-start:.0f}s)")

    if not files:
        return {"task": "picture_books", "targets": 0, "note": "no_files"}

    # 2. 比對 ALL 分頁 V 欄已知 fileId
    logger.info(f"[picture_books] 讀 ALL V 欄已知 fileId set...")
    v_res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{MAIN_SHEET}'!V2:V",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    known_fids = set()
    for r in v_res.get("values", []):
        if r and r[0]:
            known_fids.add(str(r[0]).strip())
    logger.info(f"[picture_books] ALL V 欄已有 fileId: {len(known_fids)}")

    # 3. 找 orphans
    orphans = [f for f in files if f["id"] not in known_fids]
    logger.info(f"[picture_books] Orphans: {len(orphans)}")

    if not orphans:
        return {"task": "picture_books", "targets": 0, "stats": {"orphans": 0}, "note": "no_orphans"}

    # 4. 寫 _PB_Draft
    ensure_draft_sheet(sheets_service, sid, dry_run)

    rows = [["FileId", "FileName", "MimeType", "FolderPath"]]
    for o in orphans:
        rows.append([o["id"], o["name"][:200], o["mime"], o["path"][:200]])

    if dry_run:
        logger.info(f"[picture_books] (dry_run) Would write {len(rows)} rows to {DRAFT_SHEET}")
        return {"task": "picture_books", "targets": len(orphans), "stats": {"orphans": len(orphans), "dry_run": True}}

    # clear + write
    try:
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"'{DRAFT_SHEET}'!A:D",
        ).execute()
    except HttpError:
        pass

    sheets_service.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"'{DRAFT_SHEET}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    stats = Counter({"orphans": len(orphans), "rows_written": len(rows)})
    logger.info(f"[picture_books] 完成 stats={dict(stats)}")
    return {
        "task": "picture_books",
        "targets": len(orphans),
        "elapsed_sec": int(time.time() - start),
        "stats": dict(stats),
    }
