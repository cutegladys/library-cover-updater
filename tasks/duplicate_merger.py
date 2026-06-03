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
  LIVE_MERGE                     true = 實際合併刪行；預設 false（只寫 audit log）
  FORCE_MERGE                    true = 連 manual_review (title 同/author 不同) 也合併
  MERGE_BATCH_SIZE               單次最多合併幾組（預設 30）
  SCAN_MAX_DAILY_ATTEMPTS        當日最多嘗試次數（預設 5、超過自動停 + Telegram）
  SCAN_MAX_NO_PROGRESS_STREAK    連續多少次無進展自動停（預設 3）

安全閘記錄寫到 _MergeAuditLog Z1 cell（JSON：date / attempts_today / last_succeeded / no_progress_streak）。
"""
import json
import logging
import os
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.sheets import sheet_id
from utils.notify import notify_error
from tasks.duplicate_detector import scan_duplicate_groups

logger = logging.getLogger("library_cover_updater.duplicate_merger")

MAIN_SHEET = "ALL"
AUDIT_SHEET = "_MergeAuditLog"
BACKUP_SHEET = "_MergeDeletedRows_Backup"
SAFETY_CELL = f"'{AUDIT_SHEET}'!Z1"

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

    # 治本防呆：刪重複列前，把被刪整列完整內容備份到 _MergeDeletedRows_Backup
    #（過去只記列號到 _MergeAuditLog、合併刪錯救不回；比照 GAS duplicateMergeService.js 同分頁）。
    # 備份失敗會 raise → merge_one_group 例外 → 本組不執行 delete（fail-safe：沒備份就不刪）。
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    backup_rows = []
    for row_num, data in items[1:]:
        title = str(data[0]) if data else ""
        backup_rows.append([now_str, row_num, primary_row, title] + list(data))
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"'{BACKUP_SHEET}'!A1",
        valueInputOption="RAW",
        body={"values": backup_rows},
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


def _read_safety_state(sheets_service, sid):
    try:
        r = sheets_service.spreadsheets().values().get(
            spreadsheetId=sid, range=SAFETY_CELL,
        ).execute()
        vals = r.get("values", [])
        if vals and vals[0] and vals[0][0]:
            return json.loads(str(vals[0][0]))
    except (HttpError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _write_safety_state(sheets_service, sid, state):
    try:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid, range=SAFETY_CELL,
            valueInputOption="RAW",
            body={"values": [[json.dumps(state, ensure_ascii=False)]]},
        ).execute()
    except HttpError as e:
        logger.warning(f"  safety state write err: {e}")


def run():
    live_merge = os.environ.get("LIVE_MERGE", "").lower() in ("1", "true", "yes")
    force_merge = os.environ.get("FORCE_MERGE", "").lower() in ("1", "true", "yes")
    batch_size = int(os.environ.get("MERGE_BATCH_SIZE", "30"))
    max_daily = int(os.environ.get("SCAN_MAX_DAILY_ATTEMPTS", "5"))
    max_no_progress = int(os.environ.get("SCAN_MAX_NO_PROGRESS_STREAK", "3"))
    # sanity gate：單次合併刪除筆數佔全表比例 / 絕對值異常高就中止（防大量誤刪，尤其 FORCE_MERGE 路徑）
    max_delete_ratio = float(os.environ.get("MERGE_MAX_DELETE_RATIO", "0.30"))
    max_delete_abs = int(os.environ.get("MERGE_MAX_DELETE_ABS", "100"))

    creds = load_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    # 確保 audit sheet 存在（讀 safety state 前要先確保）
    ensure_sheet(sheets_service, sid, AUDIT_SHEET)
    # 確保刪除備份分頁存在（merge_one_group 刪前 append 整列）
    ensure_sheet(sheets_service, sid, BACKUP_SHEET)

    # ── 安全閘 ──
    today = time.strftime("%Y-%m-%d")
    state = _read_safety_state(sheets_service, sid)
    if state.get("date") != today:
        state = {"date": today, "attempts_today": 0, "no_progress_streak": state.get("no_progress_streak", 0)}

    if state.get("attempts_today", 0) >= max_daily:
        msg = f"⚠️ Library merger 今日已嘗試 {state['attempts_today']} 次（上限 {max_daily}）、自動停止。明日重置或 user 手動清 _MergeAuditLog Z1。"
        logger.warning(f"[duplicate_merger] {msg}")
        notify_error(msg)
        return {"task": "duplicate_merger", "targets": 0, "note": "daily_cap_reached", "stats": dict(state)}

    if state.get("no_progress_streak", 0) >= max_no_progress:
        msg = f"⚠️ Library merger 連續 {state['no_progress_streak']} 次無進展（上限 {max_no_progress}）、自動停止。需 user 介入 review _DupCandidates 後手動清 _MergeAuditLog Z1。"
        logger.warning(f"[duplicate_merger] {msg}")
        notify_error(msg)
        return {"task": "duplicate_merger", "targets": 0, "note": "no_progress_cap", "stats": dict(state)}

    state["attempts_today"] = state.get("attempts_today", 0) + 1

    # 重跑 detector 拿最新 auto_mergeable groups（避免 stale candidates）
    logger.info("[duplicate_merger] 重跑 detector 拿最新 groups...")
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{MAIN_SHEET}'!A2:I",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    all_data = res.get("values", [])
    groups = scan_duplicate_groups(all_data)
    auto_groups = list(groups["auto_mergeable"])
    if force_merge:
        # manual_review 也納入（同 title 不同 author、master 取最小 row、其他刪）
        # safety check 跑 merge_one_group 時會以 master title 對比；既然 title.lower 都相同、會通過。
        forced = groups["manual_review"]
        logger.info(f"[duplicate_merger] FORCE_MERGE=true、額外納入 {len(forced)} 組 manual_review")
        auto_groups.extend(forced)
    logger.info(f"[duplicate_merger] 待合併: {len(auto_groups)}, LIVE_MERGE={live_merge}, FORCE_MERGE={force_merge}, batch={batch_size}, attempts_today={state['attempts_today']}/{max_daily}")

    if not auto_groups:
        # 無進展但本次無事可做也算 streak +1（avoid infinite poll-loop）
        state["no_progress_streak"] = state.get("no_progress_streak", 0) + 1
        _write_safety_state(sheets_service, sid, state)
        return {"task": "duplicate_merger", "targets": 0, "note": "no_duplicates", "stats": dict(state)}

    # sort by max row_number desc（從尾部開始合併、刪除 index 不會 shift 影響前面）
    auto_groups.sort(key=lambda g: max(g["row_numbers"]), reverse=True)
    batch = auto_groups[:batch_size]

    # ── sanity gate：本批計畫刪除筆數異常高就中止 ──
    # 防 detector 因索引/資料異常炸出超量合併組（尤其 FORCE_MERGE 把 manual_review 也納入時）造成大量誤刪。
    # 只在真的會刪（live_merge）時擋；dry_run 不擋。
    planned_deletions = sum(len(g["row_numbers"]) - 1 for g in batch)
    total_rows = len(all_data)
    delete_ratio = (planned_deletions / total_rows) if total_rows else 0.0
    if live_merge and (delete_ratio > max_delete_ratio or planned_deletions > max_delete_abs):
        force_tag = " FORCE_MERGE" if force_merge else ""
        msg = (
            f"⚠️ Library merger sanity gate 中止[LIVE{force_tag}]：本批計畫刪除 {planned_deletions} 筆"
            f"（佔全表 {total_rows} 筆的 {delete_ratio:.0%}），超過門檻"
            f"（ratio>{max_delete_ratio:.0%} 或絕對值>{max_delete_abs}）。\n"
            f"待合併組數 {len(batch)}（auto {len(auto_groups)}）。\n"
            f"未執行任何合併/刪除。請先 review _DupCandidates／detector 是否異常，"
            f"確認無誤再調高 MERGE_MAX_DELETE_RATIO / MERGE_MAX_DELETE_ABS 或分批 MERGE_BATCH_SIZE 重跑。"
        )
        logger.warning(f"[duplicate_merger] {msg}")
        notify_error(msg)
        _write_safety_state(sheets_service, sid, state)
        return {
            "task": "duplicate_merger",
            "targets": 0,
            "note": "sanity_gate_abort",
            "planned_deletions": planned_deletions,
            "total_rows": total_rows,
            "delete_ratio": round(delete_ratio, 4),
            "stats": dict(state),
        }

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

    # 更新 safety state
    if live_merge and stats.get("succeeded", 0) > 0:
        state["no_progress_streak"] = 0
        state["last_succeeded"] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        state["no_progress_streak"] = state.get("no_progress_streak", 0) + 1
    _write_safety_state(sheets_service, sid, state)

    # 通知
    mode = "LIVE" if live_merge else "DRY_RUN"
    force_tag = " FORCE" if force_merge else ""
    msg = (
        f"📋 Library merger [{mode}{force_tag}]：處理 {len(batch)} 組\n"
        f"  succeeded: {stats['succeeded']}\n"
        f"  failed: {stats['failed']}\n"
        f"  exception: {stats['exception']}\n"
        f"  attempts_today: {state['attempts_today']}/{max_daily}\n"
        f"  no_progress_streak: {state.get('no_progress_streak', 0)}/{max_no_progress}\n"
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
        "force_merge": force_merge,
        "safety_state": dict(state),
    }
