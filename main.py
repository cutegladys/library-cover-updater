#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Library Cover Updater — Zeabur entry point

統一管理所有 Library 維護 task 的排程：
- cover_drive (Phase 0)：週日 02:00 TW — Drive 檔來源 PyMuPDF/EPUB/thumbnail

未來會加：
- cover_web (Phase 1)：週一 02:00 TW — 網路搜尋封面
- duplicate_detector (Phase 2)：週三 02:00 TW — 重複偵測
- duplicate_merger (Phase 2)：Telegram button 觸發
- folder_sync (Phase 3)：每日 12:00 TW
- picture_books / inbox_processor / auto_fill_links / health_dashboard (Phase 4)

env 通用：
  GOOGLE_USER_TOKEN_JSON    主帳號 OAuth refresh token (必填)
  LIBRARY_SHEET_ID          Library 試算表 ID (必填)
  COVER_ART_FOLDER_ID       Cover Art 資料夾 ID (必填)
  TELEGRAM_BOT_TOKEN        通知 Bot Token (必填)
  TELEGRAM_CHAT_ID          通知 chat id (必填)
  COVER_UPDATER_DRY_RUN     1 = 只印不寫 sheet（除錯）
  RUN_ON_START              1 = 容器啟動立即跑所有 task 一次（部署驗證用）
  NOTIFY_MODE               always | error_only (預設 error_only)

task 各自 env：
  MAX_ROWS_PER_RUN          cover_drive / cover_web 單次最多筆數（預設 200）
"""
import logging
import os
import sys
import time
import traceback

import schedule

from utils.notify import notify_error, notify_success

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("library_cover_updater")


# ── task wrappers ──────────────────────────────────────────────

def _run_task(task_name: str, task_module_run):
    """跑一個 task；try/except 包住、發 Telegram on error。"""
    try:
        result = task_module_run()
        if result.get("note") == "no_new_books":
            msg = f"ℹ️ [{task_name}] 本次無新書"
        else:
            stats = result.get("stats", {})
            lines = [
                f"✅ [{task_name}] 完成（targets={result.get('targets',0)}, elapsed={result.get('elapsed_sec',0)}s）",
            ]
            for k, v in sorted(stats.items(), key=lambda x: -x[1]):
                lines.append(f"  {k}: {v}")
            msg = "\n".join(lines)
        logger.info(msg)
        notify_success(msg)  # error_only mode 會被略過
    except Exception as e:
        err = f"❌ [{task_name}] 失敗：{e}\n\n{traceback.format_exc()[:1500]}"
        logger.error(err)
        notify_error(f"❌ [{task_name}] 失敗：{e}")


def job_cover_drive():
    from tasks import cover_drive
    _run_task("cover_drive", cover_drive.run)


def job_cover_web():
    from tasks import cover_web
    _run_task("cover_web", cover_web.run)


def job_auto_fill_links():
    from tasks import auto_fill_links
    _run_task("auto_fill_links", auto_fill_links.run)


def job_folder_sync():
    from tasks import folder_sync
    _run_task("folder_sync", folder_sync.run)


def job_health_dashboard():
    from tasks import health_dashboard
    _run_task("health_dashboard", health_dashboard.run)


def job_inbox_processor():
    from tasks import inbox_processor
    _run_task("inbox_processor", inbox_processor.run)


def job_picture_books():
    from tasks import picture_books
    _run_task("picture_books", picture_books.run)


def job_duplicate_detector():
    from tasks import duplicate_detector
    _run_task("duplicate_detector", duplicate_detector.run)


def job_duplicate_merger():
    from tasks import duplicate_merger
    _run_task("duplicate_merger", duplicate_merger.run)


def job_merge_queue_poller():
    """D2 bridge — 不用 _run_task wrapper（成功也不發通知；merge_queue_poller 自己控制）。"""
    try:
        from tasks import merge_queue_poller
        merge_queue_poller.run()
    except Exception as e:
        from utils.notify import notify_error
        logger.error(f"merge_queue_poller exception: {e}")
        notify_error(f"❌ merge_queue_poller 異常：{e}")


# ── schedule registration ──────────────────────────────────────

def register_schedules():
    """註冊所有 task 排程。錯開時段避免互衝。"""
    # cover_drive：週六 18:00 UTC = 週日 02:00 TW（既有）
    schedule.every().saturday.at(
        os.environ.get("COVER_DRIVE_AT_UTC", "18:00")
    ).do(job_cover_drive)

    # cover_web：週日 18:00 UTC = 週一 02:00 TW（Phase 1）
    schedule.every().sunday.at(
        os.environ.get("COVER_WEB_AT_UTC", "18:00")
    ).do(job_cover_web)

    # auto_fill_links：每日 22:00 UTC = 每日 06:00 TW（Phase 4-3）
    schedule.every().day.at(
        os.environ.get("AUTO_FILL_LINKS_AT_UTC", "22:00")
    ).do(job_auto_fill_links)

    # folder_sync：每日 04:00 UTC = 每日 12:00 TW（Phase 3）
    schedule.every().day.at(
        os.environ.get("FOLDER_SYNC_AT_UTC", "04:00")
    ).do(job_folder_sync)

    # health_dashboard：每日 15:00 UTC = 每日 23:00 TW（Phase 4-4）
    schedule.every().day.at(
        os.environ.get("HEALTH_DASHBOARD_AT_UTC", "15:00")
    ).do(job_health_dashboard)

    # inbox_processor：每日 01:00 UTC = 每日 09:00 TW（Phase 4-2，原 GAS 時間）
    schedule.every().day.at(
        os.environ.get("INBOX_PROCESSOR_AT_UTC", "01:00")
    ).do(job_inbox_processor)

    # picture_books：週四 18:00 UTC = 週五 02:00 TW（Phase 4-1，原 GAS 每日 → Python 容器無 timeout 改週跑）
    schedule.every().thursday.at(
        os.environ.get("PICTURE_BOOKS_AT_UTC", "18:00")
    ).do(job_picture_books)

    # duplicate_detector：週二 18:00 UTC = 週三 02:00 TW（Phase 2）
    schedule.every().tuesday.at(
        os.environ.get("DUPLICATE_DETECTOR_AT_UTC", "18:00")
    ).do(job_duplicate_detector)

    # merge_queue_poller：每 5 分鐘 poll _MergeQueue 分頁 A1（D2 Telegram bridge 後端）
    # GAS 收到 Telegram callback "lib_merge_now" → 寫 _MergeQueue A1=pending
    # → 此 poller 每 5 分鐘看一次 → pending 觸發 duplicate_merger（LIVE_MERGE 強制）
    schedule.every(
        int(os.environ.get("MERGE_QUEUE_POLL_MIN", "5"))
    ).minutes.do(job_merge_queue_poller)

    # duplicate_merger：不自動排程；由 merge_queue_poller 透過 D2 bridge 觸發、
    # 或 LIVE_MERGE=true + RUN_ON_START=true Redeploy 手動觸發。


def main():
    register_schedules()
    logger.info("Library Cover Updater 啟動")
    for j in schedule.jobs:
        logger.info(f"  排程：{j}")

    if os.environ.get("RUN_ON_START", "false").lower() in ("1", "true", "yes"):
        logger.info("RUN_ON_START=true，立即跑一次所有 task")
        job_cover_drive()
        job_cover_web()
        job_auto_fill_links()
        job_folder_sync()
        job_health_dashboard()
        job_inbox_processor()
        job_picture_books()
        job_duplicate_detector()
        # duplicate_merger 受 LIVE_MERGE env 控制、不放在這
        if os.environ.get("LIVE_MERGE", "").lower() in ("1", "true", "yes"):
            logger.info("LIVE_MERGE=true，跑 duplicate_merger 一次")
            job_duplicate_merger()

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
