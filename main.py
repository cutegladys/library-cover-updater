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
  GOOGLE_SA_JSON            Service Account JSON 或其 base64（必填）
  RELAY_URL                 MarukoRestrictedRelay 固定 URL（可選，有內建預設）
  RELAY_TOKEN               owner-write relay token（必填）
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
            # stats 值不保證是數字（早退路徑會回 dict(state)，含 date / last_succeeded 等字串）；
            # 數字降冪排前、非數字維持插入序排後，避免對字串做 unary minus 炸掉整個成功訊息。
            def _stat_sort_key(item):
                v = item[1]
                return (0, -v) if isinstance(v, (int, float)) else (1, 0)
            for k, v in sorted(stats.items(), key=_stat_sort_key):
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


def job_review_epub_processor():
    from tasks import review_epub_processor
    _run_task("review_epub_processor", review_epub_processor.run)


def job_duplicate_detector():
    from tasks import duplicate_detector
    _run_task("duplicate_detector", duplicate_detector.run)


def job_pb_commit():
    from tasks import pb_commit
    _run_task("pb_commit", pb_commit.run)


def job_drive_index():
    from tasks import drive_index
    _run_task("drive_index", drive_index.run)


def job_duplicate_merger():
    from tasks import duplicate_merger
    _run_task("duplicate_merger", duplicate_merger.run)


def job_quarantine_cleanup():
    from tasks import quarantine_cleanup
    _run_task("quarantine_cleanup", quarantine_cleanup.run)


# merge_queue_poller 連續失敗計數：單次網路 blip（read timeout 等）會自癒——
# poller 每 5 分鐘跑、_MergeQueue A1 旗標維持 pending 直到成功處理，下個週期自動接手。
# 故依 CLAUDE.md §七 rule 17「只在無法推進時通知」，單次失敗靜默，連續 N 次才發 Telegram。
_merge_poll_consecutive_failures = 0


# inbox_queue_poller 連續失敗計數：同 merge_queue_poller —— 單次網路 blip
# （read timeout 等）會自癒。_InboxQueue A1 旗標維持 pending 直到成功處理，
# 下個 2 分鐘週期自動接手；故單次失敗靜默，連續 N 次才發 Telegram（§七 rule 17）。
_inbox_poll_consecutive_failures = 0


def job_inbox_queue_poller():
    """/ebook-inbox 一跑橋 —— 不用 _run_task wrapper（單次網路逾時不通知）。

    單次失敗（多為短暫網路逾時、OAuth refresh / Sheets read timeout）不通知——
    poller 自癒、下個 INBOX_QUEUE_POLL_MIN 分鐘週期重試。
    連續 >= INBOX_QUEUE_FAIL_NOTIFY_THRESHOLD 次（預設 3）才發 Telegram。
    注意：真正的 task 失敗在 inbox_queue_poller.run() 內已自行寫 failed_* + notify_error
    （actionable），這裡只處理「還沒進到處理就掛掉」（creds / build / read timeout）的網路類失敗。
    """
    global _inbox_poll_consecutive_failures
    try:
        from tasks import inbox_queue_poller
        inbox_queue_poller.run()
        _inbox_poll_consecutive_failures = 0
    except Exception as e:
        _inbox_poll_consecutive_failures += 1
        n = _inbox_poll_consecutive_failures
        logger.error(f"inbox_queue_poller exception (#{n}): {e}")
        threshold = int(os.environ.get("INBOX_QUEUE_FAIL_NOTIFY_THRESHOLD", "3"))
        if n >= threshold:
            from utils.notify import notify_error
            poll_min = int(os.environ.get("INBOX_QUEUE_POLL_MIN", "2"))
            notify_error(
                f"❌ inbox_queue_poller 連續 {n} 次異常"
                f"（約 {n * poll_min} 分鐘無法推進）：{e}"
            )


def job_merge_queue_poller():
    """D2 bridge — 不用 _run_task wrapper（成功也不發通知；merge_queue_poller 自己控制）。

    單次失敗（多為短暫網路逾時）不通知——poller 自癒、下個 5 分鐘週期重試。
    連續 >= MERGE_QUEUE_FAIL_NOTIFY_THRESHOLD 次（預設 3，約 15 分鐘卡死）才發 Telegram。
    注意：真正的 merge 失敗在 merge_queue_poller.run() 內已自行 notify_error（actionable），
    這裡只處理「還沒進到處理就掛掉」（creds / build / read timeout）的網路類失敗。
    """
    global _merge_poll_consecutive_failures
    try:
        from tasks import merge_queue_poller
        merge_queue_poller.run()
        _merge_poll_consecutive_failures = 0
    except Exception as e:
        _merge_poll_consecutive_failures += 1
        n = _merge_poll_consecutive_failures
        logger.error(f"merge_queue_poller exception (#{n}): {e}")
        threshold = int(os.environ.get("MERGE_QUEUE_FAIL_NOTIFY_THRESHOLD", "3"))
        if n >= threshold:
            from utils.notify import notify_error
            poll_min = int(os.environ.get("MERGE_QUEUE_POLL_MIN", "5"))
            notify_error(
                f"❌ merge_queue_poller 連續 {n} 次異常"
                f"（約 {n * poll_min} 分鐘無法推進）：{e}"
            )


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

    # ⚠️ 以下 2 個 task 已搬回主帳號 GAS（2026-05-21 對焦後決策）：
    #   auto_fill_links / health_dashboard
    # 理由：純 Sheets / 0 UrlFetch、Zeabur 沒比較好。程式碼留作備援、不註冊 schedule。
    # 對應 GAS：restoreHealthyTriggers.js → restoreHealthyLibraryTriggers()
    #
    # 註：inbox_processor 於 2026-05-27 重新啟用、但只跑 PDF 分流：
    #   - GAS Library/inboxProcessor.js 16:00 TW 跑 epub
    #   - Python 16:30 TW 跑 pdf（PyMuPDF metadata + 封面渲染、GAS 做不到）
    # 兩端各自副檔名 filter、不會重複處理。
    schedule.every().day.at(
        os.environ.get("INBOX_PROCESSOR_AT_UTC", "08:30")  # 08:30 UTC = 16:30 TW
    ).do(job_inbox_processor)

    # review_epub_processor：每日 09:00 UTC = 17:00 TW
    #   接 GAS（16:00 epub）解不開、punt 到 eBookReading/_inbox/_review 的大 epub。
    #   排在 GAS 16:00 與 Python pdf 16:30 之後，確保 GAS 已把大檔 punt 進 _review。
    #   只收 _review 的 .epub；與 GAS（_inbox epub）/ inbox_processor（_inbox pdf）來源不重疊。
    schedule.every().day.at(
        os.environ.get("REVIEW_EPUB_AT_UTC", "09:00")
    ).do(job_review_epub_processor)

    # folder_sync：每日 04:00 UTC = 每日 12:00 TW（Phase 3）
    schedule.every().day.at(
        os.environ.get("FOLDER_SYNC_AT_UTC", "04:00")
    ).do(job_folder_sync)

    # drive_index：週四 17:00 UTC = 週五 01:00 TW（picture_books 前 1 小時、提供最新 Drive 檔案清單）
    schedule.every().thursday.at(
        os.environ.get("DRIVE_INDEX_AT_UTC", "17:00")
    ).do(job_drive_index)

    # picture_books：週四 18:00 UTC = 週五 02:00 TW（Phase 4-1，原 GAS 每日 → Python 容器無 timeout 改週跑）
    schedule.every().thursday.at(
        os.environ.get("PICTURE_BOOKS_AT_UTC", "18:00")
    ).do(job_picture_books)

    # pb_commit：每日 00:00 UTC = 每日 08:00 TW（picture_books 跑完隔天早上、user 一夜手動標完 APPROVED 後自動 commit）
    schedule.every().day.at(
        os.environ.get("PB_COMMIT_AT_UTC", "00:00")
    ).do(job_pb_commit)

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

    # inbox_queue_poller：每 INBOX_QUEUE_POLL_MIN 分鐘 poll _InboxQueue 分頁 A1（/ebook-inbox 一跑橋）
    # GAS 收到 HTTP action enqueueZeaburTasks → 寫 A1="pending:inbox_processor,picture_books"
    # → 此 poller 撿起 → 即時跑 Kobo pdf 落根 / 讀本草稿，不必等 16:30 / 週四排程。
    schedule.every(
        int(os.environ.get("INBOX_QUEUE_POLL_MIN", "2"))
    ).minutes.do(job_inbox_queue_poller)

    # duplicate_merger：不自動排程；由 merge_queue_poller 透過 D2 bridge 觸發、
    # 或 LIVE_MERGE=true + RUN_ON_START=true Redeploy 手動觸發。

    # quarantine_cleanup：週日 20:00 UTC = 週一 04:00 TW（cover_web 18:00 後，錯開）
    # 只清「md5+size+ext 與非隔離存活檔完全相同」且進隔離 >MIN_AGE_DAYS 天的，丟垃圾桶可救回。
    # 預設 QUARANTINE_CLEANUP_APPLY=false（只報告）；確認首週報告無誤後設 true 才真清。
    schedule.every().sunday.at(
        os.environ.get("QUARANTINE_CLEANUP_AT_UTC", "20:00")
    ).do(job_quarantine_cleanup)


def main():
    register_schedules()
    logger.info("Library Cover Updater 啟動")
    for j in schedule.jobs:
        logger.info(f"  排程：{j}")

    if os.environ.get("RUN_ON_START", "false").lower() in ("1", "true", "yes"):
        logger.info("RUN_ON_START=true，立即跑一次所有 task")
        job_cover_drive()
        job_cover_web()
        # auto_fill_links / health_dashboard 已搬回主帳號 GAS、Zeabur 不跑
        job_inbox_processor()  # 2026-05-27 重啟、只跑 PDF
        job_review_epub_processor()  # 接 GAS 解不開 punt 到 _review 的大 epub
        job_folder_sync()
        job_drive_index()
        job_picture_books()
        job_pb_commit()
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
