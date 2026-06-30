"""
Inbox Queue Poller — /ebook-inbox 一跑到底 的 Zeabur 端即時觸發器

每 INBOX_QUEUE_POLL_MIN 分鐘（預設 2）poll Library 試算表 _InboxQueue 分頁的 A1 cell。

流程：
- A1 = "pending:<task1>,<task2>" → 依序跑各 task（白名單）→ 寫 A1 = "done_<timestamp>"
- A1 = "done_*" / "failed_*" / 空 → 不動

可觸發的 task（白名單）：
- inbox_processor       Kobo eBookReading/_inbox 的 pdf 即時落根 + 封面 + 寫 ALL
- picture_books         英文讀本資源 掃 Drive 孤兒 → _PB_Draft 草稿（讀本當場 review 用）
- review_epub_processor Kobo eBookReading/_inbox/_review 的大 epub（GAS 解不開 punt 來的）即時補編目落根

GAS 端：Library doPost 收到 HTTP action enqueueZeaburTasks 時寫 A1="pending:..."
（見 libraryHttpActions.js _enqueueZeaburTasks）。/ebook-inbox skill 前置路由完、
把書餵進對的 _inbox 後，POST enqueueZeaburTasks 觸發本 poller，不必等 16:30 / 週四排程。

設計：與 merge_queue_poller(_MergeQueue) 同一套 sheet-flag bridge，零新對外端點
（不替 cron 容器開 port / 配 domain）。延遲 = poll 間隔（user 已接受「要等」）。
"""
import logging
import os
import random
import socket
import ssl
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.sheets import sheet_id
from utils.notify import notify, notify_error

logger = logging.getLogger("library_cover_updater.inbox_queue_poller")

QUEUE_SHEET = "_InboxQueue"
QUEUE_CELL = f"'{QUEUE_SHEET}'!A1"

# 與 GAS libraryHttpActions.js ZEABUR_TASK_WHITELIST 對齊（雙端白名單、防誤觸）
TASK_WHITELIST = ("inbox_processor", "picture_books", "review_epub_processor")

_RETRYABLE_STATUSES = (409, 429, 500, 502, 503, 504)


def _execute_with_retry(request, *, label: str, retry_times: int = 3, retry_delay: float = 1.5):
    """googleapiclient .execute()，遇 transient (409/429/5xx/網路) 自動 retry。"""
    last_exc = None
    for attempt in range(retry_times + 1):
        try:
            return request.execute()
        except HttpError as e:
            last_exc = e
            if e.resp.status not in _RETRYABLE_STATUSES:
                raise
            if attempt < retry_times:
                sleep_s = retry_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"[inbox_queue_poller] {label} HTTP {e.resp.status} retry {attempt+1}/{retry_times} after {sleep_s:.1f}s")
                time.sleep(sleep_s)
        except (socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, OSError) as e:
            last_exc = e
            if attempt < retry_times:
                sleep_s = retry_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"[inbox_queue_poller] {label} network {type(e).__name__}: {e} retry {attempt+1}/{retry_times} after {sleep_s:.1f}s")
                time.sleep(sleep_s)
            else:
                raise
    raise last_exc


def ensure_queue_sheet(sheets_service, sid):
    meta = _execute_with_retry(
        sheets_service.spreadsheets().get(spreadsheetId=sid),
        label="ensure_queue_sheet.get",
    )
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == QUEUE_SHEET:
            return
    _execute_with_retry(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": QUEUE_SHEET}}}]},
        ),
        label="ensure_queue_sheet.batchUpdate",
    )


def read_queue_status(sheets_service, sid):
    try:
        r = _execute_with_retry(
            sheets_service.spreadsheets().values().get(spreadsheetId=sid, range=QUEUE_CELL),
            label="read_queue_status",
        )
        vals = r.get("values", [])
        if vals and vals[0]:
            return str(vals[0][0]).strip()
        return ""
    except HttpError as e:
        if e.resp.status == 400:
            return None  # 分頁不存在
        raise


def write_queue_status(sheets_service, sid, status: str):
    _execute_with_retry(
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid, range=QUEUE_CELL,
            valueInputOption="RAW", body={"values": [[status]]},
        ),
        label="write_queue_status",
    )


def _parse_tasks(status: str):
    """'pending:inbox_processor,picture_books' → ['inbox_processor','picture_books']（只留白名單）。"""
    body = status.split(":", 1)[1] if ":" in status else ""
    out = []
    for t in body.split(","):
        t = t.strip()
        if t and t in TASK_WHITELIST and t not in out:
            out.append(t)
    return out


def _run_one(task_name: str):
    """跑單一 task，回傳其 run() 結果 dict。inbox_processor/picture_books 都吃 COVER_UPDATER_DRY_RUN env。"""
    if task_name == "inbox_processor":
        from tasks import inbox_processor
        return inbox_processor.run()
    if task_name == "picture_books":
        from tasks import picture_books
        return picture_books.run()
    if task_name == "review_epub_processor":
        from tasks import review_epub_processor
        return review_epub_processor.run()
    raise ValueError(f"未知 task：{task_name}")


def run():
    creds = load_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    try:
        ensure_queue_sheet(sheets_service, sid)
    except HttpError as e:
        logger.warning(f"[inbox_queue_poller] ensure queue sheet err: {e}")
        return {"task": "inbox_queue_poller", "targets": 0, "note": "ensure_sheet_err"}

    status = read_queue_status(sheets_service, sid)
    if status is None:
        return {"task": "inbox_queue_poller", "targets": 0, "note": "no_queue_sheet"}

    if not status.startswith("pending"):
        logger.info(f"[inbox_queue_poller] queue status: '{status}' (skip)")
        return {"task": "inbox_queue_poller", "targets": 0, "note": "no_pending"}

    tasks = _parse_tasks(status)
    if not tasks:
        # pending 但 task 全不在白名單 → 清成 done 免卡死，並通知（actionable）
        write_queue_status(sheets_service, sid, f"done_{time.strftime('%Y-%m-%d %H:%M:%S')}")
        notify_error(f"⚠ inbox_queue_poller：queue '{status}' 無有效 task（白名單 {TASK_WHITELIST}），已清空")
        return {"task": "inbox_queue_poller", "targets": 0, "note": "no_valid_task"}

    logger.info(f"[inbox_queue_poller] queue={status}，依序跑 {tasks}")
    results, errors = {}, {}
    for t in tasks:
        try:
            results[t] = _run_one(t)
            logger.info(f"[inbox_queue_poller] {t} 完成：{results[t]}")
        except Exception as e:
            errors[t] = str(e)
            logger.error(f"[inbox_queue_poller] {t} 失敗：{e}")

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    if errors:
        write_queue_status(sheets_service, sid, f"failed_{ts}")
        notify_error("❌ /ebook-inbox 即時觸發部分失敗：\n" +
                     "\n".join(f"  {k}: {v}" for k, v in errors.items()))
    else:
        write_queue_status(sheets_service, sid, f"done_{ts}")
        lines = ["✅ /ebook-inbox 即時觸發完成"]
        for t in tasks:
            stats = (results.get(t) or {}).get("stats", {})
            lines.append(f"  {t}: targets={(results.get(t) or {}).get('targets', 0)}"
                         + (f" {stats}" if stats else ""))
        notify("\n".join(lines), force=True)

    return {
        "task": "inbox_queue_poller",
        "targets": len(tasks),
        "stats": {t: ((results.get(t) or {}).get("targets", 0)) for t in tasks},
    }
