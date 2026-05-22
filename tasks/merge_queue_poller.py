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
import random
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id
from utils.notify import notify, notify_error

logger = logging.getLogger("library_cover_updater.merge_queue_poller")

QUEUE_SHEET = "_MergeQueue"
QUEUE_CELL = f"'{QUEUE_SHEET}'!A1"

# Sheets transient errors — 409 (concurrent edit / aborted), 429 (rate), 5xx
_RETRYABLE_STATUSES = (409, 429, 500, 502, 503, 504)


def _execute_with_retry(request, *, label: str, retry_times: int = 3, retry_delay: float = 1.5):
    """
    對 googleapiclient request 呼叫 .execute()，遇 transient (409/429/5xx) 自動 retry。
    其他 HttpError 直接 raise。retry 全部用完仍失敗 → raise 最後一次的例外。
    """
    last_exc = None
    for attempt in range(retry_times + 1):
        try:
            return request.execute()
        except HttpError as e:
            last_exc = e
            status = e.resp.status
            if status not in _RETRYABLE_STATUSES:
                raise
            if attempt < retry_times:
                sleep_s = retry_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    f"[merge_queue_poller] {label} HTTP {status} retry "
                    f"{attempt+1}/{retry_times} after {sleep_s:.1f}s"
                )
                time.sleep(sleep_s)
    raise last_exc


def ensure_queue_sheet(sheets_service, spreadsheet_id):
    """確保 _MergeQueue 分頁存在。"""
    meta = _execute_with_retry(
        sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id),
        label="ensure_queue_sheet.get",
    )
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == QUEUE_SHEET:
            return
    _execute_with_retry(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": QUEUE_SHEET}}}]},
        ),
        label="ensure_queue_sheet.batchUpdate",
    )


def read_queue_status(sheets_service, sid):
    """讀 A1 cell 字串。"""
    try:
        r = _execute_with_retry(
            sheets_service.spreadsheets().values().get(
                spreadsheetId=sid, range=QUEUE_CELL,
            ),
            label="read_queue_status",
        )
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
    _execute_with_retry(
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid, range=QUEUE_CELL,
            valueInputOption="RAW",
            body={"values": [[status]]},
        ),
        label="write_queue_status",
    )


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

    if status not in ("pending", "pending_force"):
        # 沒事做（空或 done_*）— 安靜返回，不發通知
        logger.info(f"[merge_queue_poller] queue status: '{status}' (skip)")
        return {"task": "merge_queue_poller", "targets": 0, "note": "no_pending"}

    is_force = status == "pending_force"
    logger.info(f"[merge_queue_poller] queue={status}，觸發 duplicate_merger LIVE_MERGE=true{' FORCE_MERGE=true' if is_force else ''}")

    # 暫時設 env 確保 merger LIVE（及可選 FORCE）
    orig_live = os.environ.get("LIVE_MERGE", "")
    orig_force = os.environ.get("FORCE_MERGE", "")
    os.environ["LIVE_MERGE"] = "true"
    if is_force:
        os.environ["FORCE_MERGE"] = "true"

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
        if orig_force:
            os.environ["FORCE_MERGE"] = orig_force
        else:
            os.environ.pop("FORCE_MERGE", None)
