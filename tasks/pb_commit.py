"""
PB Commit — 把 _PB_Draft 中 APPROVED 列寫進 ALL + 改 COMMITTED

對應 pictureBooksProcessor.js pbCommitDraft (line 256-330)。

GAS 原邏輯：
1. 掃 _PB_Draft 每列，J 欄（操作）= APPROVED 才處理
2. 防衛跳過：title 為空 / fileId 為空 / title or folderPath 含 _duplicates_quarantine
3. 組 22 欄 new row（A 書名, B 作者, C 語言, F 來源=Google Drive, G 狀態=已擁有, U Drive URL, V FileId）
4. 一次性 append 到 ALL（lastRow+1 起）
5. 一次性把 _PB_Draft J 欄改為 COMMITTED
6. Telegram 通知 committed / skipped

下次 picture_books 跑時 reconcile 會自動清掉 COMMITTED 列（fileId 已在 ALL V 欄 → 從 orphan set 移除 → 既有 NOT in 當前 set + DONE_ACTIONS 含 COMMITTED → 刪）。

env:
  COVER_UPDATER_DRY_RUN  1 = 只印不寫
"""
import logging
import os
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.pb_commit")

DRAFT_SHEET = "_PB_Draft"
MAIN_SHEET = "ALL"

# ALL 欄位（1-based、對齊 _PB / sheetConfig）
ALL_COL_TITLE = 1
ALL_COL_AUTHOR = 2
ALL_COL_LANG = 3
ALL_COL_SOURCE = 6
ALL_COL_STATUS = 7
ALL_COL_DRIVE_URL = 21
ALL_COL_FILE_ID = 22
ALL_TOTAL_COLS = 22

SOURCE_VALUE = "Google Drive"
STATUS_VALUE = "已擁有"


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    # 讀 _PB_Draft 全部
    try:
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{DRAFT_SHEET}'!A1:J",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
    except HttpError as e:
        if e.resp.status == 400:
            return {"task": "pb_commit", "targets": 0, "note": "no_draft_sheet"}
        raise

    data = res.get("values", [])
    if len(data) < 2:
        return {"task": "pb_commit", "targets": 0, "note": "no_rows"}

    # 找 APPROVED 列
    stats = Counter()
    rows_to_commit = []  # list of (draft_row_num, new_row_22cols)

    for i, row in enumerate(data[1:], start=2):  # start row 2 (skip header)
        while len(row) < 10:
            row.append("")
        action = str(row[9] or "").strip().upper()
        if action != "APPROVED":
            stats["skipped_not_approved"] += 1
            continue

        title = str(row[0] or "").strip()
        author = str(row[1] or "").strip()
        lang = str(row[2] or "").strip()
        source = str(row[3] or "").strip() or SOURCE_VALUE
        status_val = str(row[4] or "").strip() or STATUS_VALUE
        file_id = str(row[5] or "").strip()
        url = str(row[6] or "").strip()
        folder_path = str(row[7] or "").strip()

        # 防衛 1：title / fileId 為空
        if not title or not file_id:
            logger.warning(f"  row {i}：缺 title 或 fileId、跳過")
            stats["skipped_missing"] += 1
            continue

        # 防衛 2：_duplicates_quarantine 強制跳過（即使操作=APPROVED）
        if title == "_duplicates_quarantine" or "_duplicates_quarantine" in folder_path:
            logger.warning(f"  row {i}：含 _duplicates_quarantine、強制跳過")
            stats["skipped_quarantine"] += 1
            continue

        # 組 ALL 的 22 欄 new row
        new_row = [""] * ALL_TOTAL_COLS
        new_row[ALL_COL_TITLE - 1] = title
        new_row[ALL_COL_AUTHOR - 1] = author
        new_row[ALL_COL_LANG - 1] = lang
        new_row[ALL_COL_SOURCE - 1] = source
        new_row[ALL_COL_STATUS - 1] = status_val
        new_row[ALL_COL_DRIVE_URL - 1] = url
        new_row[ALL_COL_FILE_ID - 1] = file_id

        rows_to_commit.append((i, new_row))

    committed = len(rows_to_commit)
    stats["approved_found"] = committed

    if committed == 0:
        logger.info(f"[pb_commit] 無 APPROVED 列、跳過。stats={dict(stats)}")
        return {"task": "pb_commit", "targets": 0, "note": "no_approved", "stats": dict(stats)}

    logger.info(f"[pb_commit] 找到 {committed} 個 APPROVED 列、準備 commit 到 ALL (dry_run={dry_run})")

    if dry_run:
        for row_num, new_row in rows_to_commit[:5]:
            logger.info(f"  (dry_run) row {row_num} → ALL append: {new_row[0][:40]}")
        return {"task": "pb_commit", "targets": committed, "stats": dict(stats), "dry_run": True}

    # 1. Append 到 ALL（一次性、用 append API）
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"'{MAIN_SHEET}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [r for _, r in rows_to_commit]},
    ).execute()
    logger.info(f"[pb_commit] 已 append {committed} 列到 {MAIN_SHEET}")

    # 2. 把 _PB_Draft 對應列 J 欄改 COMMITTED（一次性 batchUpdate）
    updates = []
    for draft_row_num, _ in rows_to_commit:
        updates.append({
            "range": f"'{DRAFT_SHEET}'!J{draft_row_num}",
            "values": [["COMMITTED"]],
        })
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    logger.info(f"[pb_commit] 已標 {committed} 列為 COMMITTED")

    logger.info(f"[pb_commit] 完成 stats={dict(stats)}")
    return {
        "task": "pb_commit",
        "targets": committed,
        "elapsed_sec": 0,
        "stats": dict(stats),
    }
