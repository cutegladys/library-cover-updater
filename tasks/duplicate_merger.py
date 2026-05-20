"""
Library 重複書合併 — 執行階段（dry-run 預設、LIVE_MERGE=true 才實際刪）

對應 scanForDuplicateBooks.js + duplicateMergeService.js mergeDuplicateBooksByStoredData。

流程：
1. 重新跑 detector 拿 auto_mergeable groups（不依賴 _DupCandidates 分頁，避免 stale）
2. 對每組：
   - 找 master row（最小 row_number）
   - safety check：所有 row title.lower() 一致才合併
   - merge B-H 欄空值填入；I 欄 Note 合併
   - write master row、delete 其他 rows（從大 row_number 開始刪、避免 index shift）
3. LIVE_MERGE=true 才實際寫；預設只印 plan + 寫 _MergeAuditLog 分頁

env:
  LIVE_MERGE             true = 實際合併刪行；預設 false（只寫 audit log）
  MERGE_BATCH_SIZE       單次最多合併幾組（預設 30）
"""
import logging
import os
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id
from utils.notify import notify_error
from tasks.duplicate_detector import scan_duplicate_groups

logger = logging.getLogger("library_cover_updater.duplicate_merger")

MAIN_SHEET = "ALL"
AUDIT_SHEET = "_MergeAuditLog"

# 對應 sheetConfig.js MERGE_INDICES (0-based) = [1,2,3,4,5,6,7]，NOTE_INDEX=8
MERGE_INDICES = [1, 2, 3, 4, 5, 6, 7]
NOTE_INDEX = 8


def ensure_sheet(sheets_service, spreadsheet_id, name):
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == name:
            return s["properties"]["sheetId"]
    res = sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": name}}}]},
    ).execute()
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]


def merge_one_group(sheets_service, sid, all_sheet_id, dup_group, dry_run=True):
    """
    對應 mergeDuplicateBooksByStoredData。
    回 {success, mergedRow, deletedCount, message}
    """
    items = list(zip(dup_group["row_numbers"], dup_group["rows"]))
    items.sort(key=lambda x: x[0])
    primary_row, primary_data = items[0]
    primary_title = str(primary_data[0]).strip().lower()

    # safety check
    invalid = []
    for row_num, data in items[1:]:
        cur_title = str(data[0]).strip().lower()
        if cur_title != primary_title:
            invalid.append({"row": row_num, "expected": primary_data[0], "actual": data[0]})
    if invalid:
        return {"success": False, "message": f"title 驗證失敗：{invalid}"}

    # merge metadata
    merged_data = list(primary_data)
    for row_num, row_data in items[1:]:
        for idx in MERGE_INDICES:
            if idx >= len(merged_data):
                continue
            if not str(merged_data[idx] if idx < len(merged_data) else "").strip():
                if idx < len(row_data) and row_data[idx]:
                    merged_data[idx] = row_data[idx]
        # Note 合併
        if NOTE_INDEX < len(merged_data) or NOTE_INDEX < len(row_data):
            existing = str(merged_data[NOTE_INDEX] if NOTE_INDEX < len(merged_data) else "").strip()
            incoming = str(row_data[NOTE_INDEX] if NOTE_INDEX < len(row_data) else "").strip()
            if incoming and incoming not in existing:
                while len(merged_data) <= NOTE_INDEX:
                    merged_data.append("")
                merged_data[NOTE_INDEX] = (existing + "；" + incoming) if existing else incoming

    if dry_run:
        return {
            "success": True,
            "mergedRow": primary_row,
            "deletedCount": len(items) - 1,
            "dry_run": True,
            "row_numbers_to_delete": [r[0] for r in items[1:]],
        }

    # write master row
    range_a1 = f"'{MAIN_SHEET}'!A{primary_row}"
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sid, range=range_a1,
        valueInputOption="USER_ENTERED",
        body={"values": [merged_data]},
    ).execute()

    # delete other rows（從大 row_number 開始刪、避免 index shift）
    rows_to_delete = sorted([r[0] for r in items[1:]], reverse=True)
    delete_requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": all_sheet_id,
                    "dimension": "ROWS",
                    "startIndex": r - 1,
                    "endIndex": r,
                }
            }
        }
        for r in rows_to_delete
    ]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": delete_requests},
    ).execute()

    return {
        "success": True,
        "mergedRow": primary_row,
        "deletedCount": len(rows_to_delete),
    }


def get_all_sheet_id(sheets_service, spreadsheet_id):
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == MAIN_SHEET:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"找不到 {MAIN_SHEET} 分頁")


def run():
    live_merge = os.environ.get("LIVE_MERGE", "").lower() in ("1", "true", "yes")
    batch_size = int(os.environ.get("MERGE_BATCH_SIZE", "30"))

    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    # 重跑 detector 拿最新 auto_mergeable groups（避免 stale candidates）
    logger.info("[duplicate_merger] 重跑 detector 拿最新 groups...")
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{MAIN_SHEET}'!A2:I",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    all_data = res.get("values", [])
    groups = scan_duplicate_groups(all_data)
    auto_groups = groups["auto_mergeable"]
    logger.info(f"[duplicate_merger] auto_mergeable: {len(auto_groups)}, LIVE_MERGE={live_merge}, batch={batch_size}")

    if not auto_groups:
        return {"task": "duplicate_merger", "targets": 0, "note": "no_duplicates"}

    # sort by max row_number desc（從尾部開始合併、刪除 index 不會 shift 影響前面）
    auto_groups.sort(key=lambda g: max(g["row_numbers"]), reverse=True)
    batch = auto_groups[:batch_size]

    # 確保 audit sheet 存在
    ensure_sheet(sheets_service, sid, AUDIT_SHEET)

    # 確保 ALL sheet id
    all_sheet_id = get_all_sheet_id(sheets_service, sid)

    audit_rows = [["Time", "Group", "Title", "MasterRow", "DeletedRows", "Status"]]
    stats = Counter()
    start = time.time()

    for i, g in enumerate(batch, 1):
        title = g["title"][:80]
        row_nums = g["row_numbers"]
        try:
            result = merge_one_group(sheets_service, sid, all_sheet_id, g, dry_run=not live_merge)
            if result["success"]:
                stats["succeeded"] += 1
                audit_rows.append([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    f"AUTO-{i}", title,
                    str(result["mergedRow"]),
                    ", ".join(str(r) for r in result.get("row_numbers_to_delete", []) or [r for r in row_nums if r != result["mergedRow"]]),
                    "DRY_RUN" if result.get("dry_run") else "MERGED",
                ])
                logger.info(f"  AUTO-{i} master={result['mergedRow']} deleted={result['deletedCount']} {'DRY_RUN' if result.get('dry_run') else 'MERGED'}")
            else:
                stats["failed"] += 1
                audit_rows.append([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    f"AUTO-{i}", title, "", "",
                    f"FAILED: {result.get('message','')[:80]}",
                ])
                logger.warning(f"  AUTO-{i} FAILED: {result.get('message')}")
        except Exception as e:
            stats["exception"] += 1
            logger.warning(f"  AUTO-{i} EXC: {e}")
            audit_rows.append([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                f"AUTO-{i}", title, "", "", f"EXC: {str(e)[:80]}",
            ])

    # 寫 audit log（append 不 clear，保留歷史）
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"'{AUDIT_SHEET}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": audit_rows},
        ).execute()
    except HttpError as e:
        logger.warning(f"audit log write err: {e}")

    # 通知
    mode = "LIVE" if live_merge else "DRY_RUN"
    msg = (
        f"📋 Library merger [{mode}]：處理 {len(batch)} 組\n"
        f"  succeeded: {stats['succeeded']}\n"
        f"  failed: {stats['failed']}\n"
        f"  exception: {stats['exception']}\n"
    )
    if not live_merge:
        msg += "目前是 DRY_RUN、未實際刪行。設 LIVE_MERGE=true + Redeploy 真正合併。"
    notify_error(msg)

    return {
        "task": "duplicate_merger",
        "targets": len(batch),
        "elapsed_sec": int(time.time() - start),
        "stats": dict(stats),
        "live_merge": live_merge,
    }
