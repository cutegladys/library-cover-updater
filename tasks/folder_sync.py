"""
Library 檔名同步 — 用 Drive fileId 查當前檔名、對齊 sheet A 欄

對應 syncFolderNamesToSheet (listDriveFiles.js)。

流程：
1. 讀 ALL 分頁所有列
2. 對 Source=Google Drive + U 欄有 fileId 的列：
   - drive_service.files().get(fileId).name → 當前 Drive 檔名
   - 去副檔名跟 A 欄 title 比較
   - 不同 → 加入 update queue
3. batchUpdate 寫回 A 欄新檔名

對應原 GAS 版有 lock/stop/Telegram 互動、Python 容器無需（單 instance、cron 跑完即 idle）。
原版 6 分鐘 timeout 在 Python 容器無限制、一次跑完。

env：
  COVER_UPDATER_DRY_RUN     1 = 只印不寫
  FOLDER_SYNC_MAX_PER_RUN   單次最多檢查多少列（預設 0 = 不限制；過去 GAS 受 6 分鐘 timeout 才有此參數）
"""
import logging
import os
import re
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import (
    COL_TITLE, COL_SOURCE, COL_DRIVE_LINK,
    extract_file_id, sheet_id, read_all_rows,
)

logger = logging.getLogger("library_cover_updater.folder_sync")

SOURCE_FILTER = "Google Drive"
EXT_RE = re.compile(r"\.[^.]*$")


def strip_ext(name: str) -> str:
    return EXT_RE.sub("", name).strip()


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    max_per_run = int(os.environ.get("FOLDER_SYNC_MAX_PER_RUN", "0"))

    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    sid = sheet_id()

    logger.info("[folder_sync] Loading Library sheet...")
    rows = read_all_rows(sheets_service)
    logger.info(f"[folder_sync] Total rows: {len(rows)} (dry_run={dry_run})")

    # 收集 (row_num, fileId, title) tuples 要打 Drive API 查
    candidates = []
    for idx, row in enumerate(rows, start=2):
        while len(row) <= COL_DRIVE_LINK:
            row.append("")
        title = str(row[COL_TITLE]).strip()
        source = str(row[COL_SOURCE]).strip()
        if source != SOURCE_FILTER:
            continue
        if not title:
            continue
        fid = extract_file_id(str(row[COL_DRIVE_LINK]))
        if not fid:
            continue
        candidates.append((idx, fid, title))

    logger.info(f"[folder_sync] Candidates (Source=Google Drive + has fileId): {len(candidates)}")

    if max_per_run > 0:
        candidates = candidates[:max_per_run]
        logger.info(f"[folder_sync] limited to first {max_per_run} per run")

    if not candidates:
        return {"task": "folder_sync", "targets": 0, "note": "no_new_books"}

    # 對每個 candidate 查 Drive 當前檔名
    updates = []  # batchUpdate body
    stats = Counter()
    start = time.time()

    for i, (row_num, fid, title) in enumerate(candidates, 1):
        try:
            meta = drive_service.files().get(
                fileId=fid,
                fields="name,trashed",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                stats["dead_404"] += 1
            else:
                stats[f"meta_err_{e.resp.status}"] += 1
            continue

        if meta.get("trashed"):
            stats["trashed"] += 1
            continue

        current_name = meta.get("name", "")
        if not current_name:
            continue

        current_stripped = strip_ext(current_name)
        title_stripped = strip_ext(title)

        # 兩種比對方式（對應 GAS 邏輯）：去副檔名 或 原樣
        if current_stripped == title_stripped or current_stripped == title:
            stats["unchanged"] += 1
            continue

        # 需更新：寫回 A 欄為去副檔名後的當前 Drive 名
        updates.append({
            "range": f"'ALL'!A{row_num}",
            "values": [[current_stripped]],
        })
        stats["needs_update"] += 1

        if i % 100 == 0:
            elapsed = time.time() - start
            logger.info(f"[folder_sync]   進度 {i}/{len(candidates)}  elapsed={elapsed:.0f}s  stats={dict(stats)}")

    elapsed = time.time() - start
    logger.info(f"[folder_sync] 比對完成 elapsed={elapsed:.0f}s, 待更新 {len(updates)} 筆")

    # batchUpdate 一次寫回（dry-run 時跳過）
    if updates and not dry_run:
        # batchUpdate 上限 Sheets API 預設沒嚴格上限，但 1000+ 個 range 可能超 quota，分批
        BATCH = 500
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            try:
                sheets_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sid,
                    body={
                        "valueInputOption": "USER_ENTERED",
                        "data": chunk,
                    },
                ).execute()
                stats["written"] += len(chunk)
            except HttpError as e:
                stats[f"write_err_{e.resp.status}"] += 1
                logger.warning(f"[folder_sync] batchUpdate err {e.resp.status}: {e}")

    elapsed = time.time() - start
    logger.info(f"[folder_sync] 完成 elapsed={elapsed:.0f}s stats={dict(stats)}")
    return {
        "task": "folder_sync",
        "targets": len(candidates),
        "elapsed_sec": int(elapsed),
        "stats": dict(stats),
    }
