"""
Drive Index Rebuild — 重建「檔案清單_Export」分頁

對應 driveIndex.js rebuildDriveIndex (line 53-161)。

GAS 原版邏輯：因 6 分鐘 timeout 用 BFS queue + stage sheet + checkpoint 分批續跑，
複雜度高。Python 容器無 timeout，可一次性 recursive 掃完 + 直接寫入。

掃描兩個 root folder：
- 1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx（英文讀本資源）
- 1N-CIlms9t6HHyui52682bAC-gzVmZH3H（eBookReading）

寫入「檔案清單_Export」分頁，欄位：
  A=FileName | B=FileId | C=URL | D=FolderPath | E=LastUpdated

clear + write 邏輯：dashboard / picture_books 都讀這分頁、需要最新資料。
不是「user review 中」的分頁、clear+write 是對的（與 _PB_Draft 不同）。

env:
  COVER_UPDATER_DRY_RUN  1 = 只統計、不寫
  DRIVE_INDEX_ROOT_IDS   覆寫 root folder id list（comma-separated），預設兩個
"""
import logging
import os
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.drive_index")

DEFAULT_ROOT_IDS = [
    "1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx",  # 英文讀本資源
    "1N-CIlms9t6HHyui52682bAC-gzVmZH3H",  # eBookReading
]

INDEX_SHEET = "檔案清單_Export"
PAGE_SIZE = 1000


def list_files_with_metadata(drive_service, root_id: str):
    """遞迴掃 root 下所有非 folder 檔案，回 list of {name, id, url, path, modified}。"""
    result = []
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
                    fields="nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime)",
                    pageSize=PAGE_SIZE,
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
                        "name": f.get("name", ""),
                        "id": f["id"],
                        "url": f.get("webViewLink", f"https://drive.google.com/file/d/{f['id']}/view"),
                        "path": current_path,
                        "modified": f.get("modifiedTime", ""),
                    })

            page_token = r.get("nextPageToken")
            if not page_token:
                break

    return result


def ensure_index_sheet(sheets_service, spreadsheet_id):
    """確保「檔案清單_Export」分頁存在。"""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == INDEX_SHEET:
            return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": INDEX_SHEET}}}]},
    ).execute()


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    roots_env = os.environ.get("DRIVE_INDEX_ROOT_IDS", "").strip()
    root_ids = [s.strip() for s in roots_env.split(",") if s.strip()] if roots_env else DEFAULT_ROOT_IDS

    creds = load_creds()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    start = time.time()
    all_files = []
    for root_id in root_ids:
        logger.info(f"[drive_index] 掃 root {root_id}...")
        files = list_files_with_metadata(drive_service, root_id)
        logger.info(f"  → {len(files)} 個檔案")
        all_files.extend(files)

    elapsed = time.time() - start
    logger.info(f"[drive_index] 共 {len(all_files)} 個檔案 ({elapsed:.0f}s, dry_run={dry_run})")

    if dry_run:
        return {"task": "drive_index", "targets": len(all_files), "stats": {"total": len(all_files), "dry_run": True}}

    # 確保分頁存在
    ensure_index_sheet(sheets_service, sid)

    # 組 rows
    rows = [["FileName", "FileId", "URL", "FolderPath", "LastUpdated"]]
    for f in all_files:
        rows.append([f["name"][:200], f["id"], f["url"], f["path"][:200], f["modified"]])

    # clear + write（dashboard / picture_books 讀這分頁、要最新）
    try:
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"'{INDEX_SHEET}'!A:E",
        ).execute()
    except HttpError:
        pass

    # 分批 write（避免單次 request payload 過大；每批 5000 列）
    BATCH = 5000
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        # 第一批寫入 A1，後續用 append
        if i == 0:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"'{INDEX_SHEET}'!A1",
                valueInputOption="RAW",
                body={"values": chunk},
            ).execute()
        else:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=sid,
                range=f"'{INDEX_SHEET}'!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": chunk},
            ).execute()

    stats = {"total_files": len(all_files), "rows_written": len(rows)}
    logger.info(f"[drive_index] 完成 stats={stats}")
    return {
        "task": "drive_index",
        "targets": len(all_files),
        "elapsed_sec": int(elapsed),
        "stats": stats,
    }
