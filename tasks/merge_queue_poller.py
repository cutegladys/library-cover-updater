"""
Merge Queue Poller — D2 Telegram bridge 的 Zeabur 端

每 5 分鐘 poll Library 試算表 _MergeQueue 分頁的 A1 cell。

流程：
- A1 = "pending" → 跑 duplicate_merger（強制 LIVE_MERGE=true）→ 寫 A1 = "done_<timestamp>"
- A1 = "done_*" 或空 → 不動

GAS 端：主 Library doPost 收到 callback_data="lib_merge_now" 時寫 A1="pending"。
Telegram inline button → GAS → sheet 旗標 → Zeabur poll → merger → 完成。
"""
import logging
import os
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id
from utils.notify import notify, notify_error

logger = logging.getLogger("library_cover_updater.merge_queue_poller")

QUEUE_SHEET = "_MergeQueue"
QUEUE_CELL = f"'{QUEUE_SHEET}'!A1"


def ensure_queue_sheet(sheets_service, spreadsheet_id):
    """確保 _MergeQueue 分頁存在。"""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == QUEUE_SHEET:
            return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": QUEUE_SHEET}}}]},
    ).execute()


def read_queue_status(sheets_service, sid):
    """讀 A1 cell 字串。"""
    try:
        r = sheets_service.spreadsheets().values().get(
            spreadsheetId=sid, range=QUEUE_CELL,
        ).execute()
        vals = r.get("values", [])
        if vals and vals[0]:
            return str(vals[0][0]).strip()
        return ""
    except HttpError as e:
        if e.resp.status == 400:
            # 分頁不存在
            return None
        raise


def write_queue_status(sheets_service, sid, status: str):
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sid, range=QUEUE_CELL,
        valueInputOption="RAW",
        body={"values": [[status]]},
    ).execute()


def run():
    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    # 確保 queue sheet 存在
    try:
        ensure_queue_sheet(sheets_service, sid)
    except HttpError as e:
        logger.warning(f"[merge_queue_poller] ensure queue sheet err: {e}")
        return {"task": "merge_queue_poller", "targets": 0, "note": "ensure_sheet_err"}

    status = read_queue_status(sheets_service, sid)
    if status is None:
        return {"task": "merge_queue_poller", "targets": 0, "note": "no_queue_sheet"}

    if status != "pending":
        # 沒事做（空或 done_*）— 安靜返回，不發通知
        logger.info(f"[merge_queue_poller] queue status: '{status}' (skip)")
        return {"task": "merge_queue_poller", "targets": 0, "note": "no_pending"}

    # 有 pending！跑 merger（強制 LIVE_MERGE）
    logger.info("[merge_queue_poller] queue=pending，觸發 duplicate_merger LIVE_MERGE=true")

    # 暫時設 env 確保 merger LIVE
    orig_live = os.environ.get("LIVE_MERGE", "")
    os.environ["LIVE_MERGE"] = "true"

    try:
        from tasks import duplicate_merger
        result = duplicate_merger.run()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        write_queue_status(sheets_service, sid, f"done_{ts}")
        # 通知合併結果
        stats = result.get("stats", {})
        succeeded = stats.get("succeeded", 0)
        failed = stats.get("failed", 0)
        notify(
            f"✅ Library 合併已完成（D2 bridge 觸發）\n"
            f"  succeeded: {succeeded}\n"
            f"  failed: {failed}\n"
            f"  目標: {result.get('targets', 0)} 組",
            force=True,
        )
        return {
            "task": "merge_queue_poller",
            "targets": 1,
            "stats": {"triggered_merger": True, **stats},
        }
    except Exception as e:
        write_queue_status(sheets_service, sid, f"failed_{time.strftime('%Y-%m-%d %H:%M:%S')}")
        notify_error(f"❌ D2 bridge merge 失敗：{e}")
        raise
    finally:
        # 還原 env
        if orig_live:
            os.environ["LIVE_MERGE"] = orig_live
        else:
            os.environ.pop("LIVE_MERGE", None)
