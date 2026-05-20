#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Library 封面修補 — 獨立 Zeabur 容器 entry point

每週日 02:00 TW（=Sat 18:00 UTC）跑一次 run_cover_update()。
跑完發 Telegram 通知（成功/失敗都通知）。

env：
  必填
    GOOGLE_USER_TOKEN_JSON       主帳號 OAuth refresh token 整包 minified JSON
    LIBRARY_SHEET_ID             Library 試算表 ID
    COVER_ART_FOLDER_ID          Cover Art 資料夾 ID
    TELEGRAM_BOT_TOKEN           Telegram Bot Token（成功/失敗通知）
    TELEGRAM_CHAT_ID             你的 chat id
  可選
    MAX_ROWS_PER_RUN             單次最多處理（預設 200）
    LIBRARY_COVER_UPDATER_AT_UTC 排程時間 UTC 24h 格式（預設 18:00 = 週日 02:00 TW）
    COVER_UPDATER_DRY_RUN        1 = 只印不寫（除錯）
    RUN_ON_START                 1 = 容器啟動立即跑一次（部署驗證用）
"""

import logging
import os
import sys
import time
import traceback

import schedule

from updater import run_cover_update
from notification import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("library_cover_updater")


def job():
    try:
        result = run_cover_update()
        if result.get("note") == "no_new_books":
            msg = "ℹ️ Library 封面修補：本次無新書"
        else:
            stats = result.get("stats", {})
            lines = [
                f"✅ Library 封面修補完成（Zeabur 週排程）",
                f"處理 {result['targets']} 本，耗時 {result['elapsed_sec']}s",
            ]
            for k, v in sorted(stats.items(), key=lambda x: -x[1]):
                lines.append(f"  {k}: {v}")
            msg = "\n".join(lines)
        logger.info(msg)
        notify(msg)
    except Exception as e:
        err = f"❌ Library 封面修補失敗：{e}\n\n{traceback.format_exc()}"
        logger.error(err)
        notify(f"❌ Library 封面修補失敗：{e}")


def main():
    at_utc = os.environ.get("LIBRARY_COVER_UPDATER_AT_UTC", "18:00")
    # 週六 18:00 UTC = 週日 02:00 TW
    schedule.every().saturday.at(at_utc).do(job)

    logger.info("Library Cover Updater 啟動")
    for j in schedule.jobs:
        logger.info(f"  排程：{j}")

    if os.environ.get("RUN_ON_START", "false").lower() in ("1", "true", "yes"):
        logger.info("RUN_ON_START=true，立即跑一次")
        job()

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
