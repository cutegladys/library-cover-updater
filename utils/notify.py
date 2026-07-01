"""Telegram 直連通知。error_only 模式：成功不發、失敗才發。"""
import logging
import os

import requests

logger = logging.getLogger("library_cover_updater")


def notify(message: str, *, force: bool = False) -> bool:
    """
    Telegram Bot API 發訊息。
    error_only 預設：只在 force=True（例如失敗）才實際發。
    成功摘要建議用 force=False（被 default 略過）。

    若 NOTIFY_MODE=always，所有訊息都發。
    """
    mode = os.environ.get("NOTIFY_MODE", "error_only").lower()
    if mode == "error_only" and not force:
        logger.info(f"[notify suppressed, error_only mode] {message[:100]}")
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定，跳過通知")
        logger.info(f"訊息內容：{message}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        logger.warning(f"Telegram API HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Telegram 通知例外：{e}")
        return False


def notify_error(message: str) -> bool:
    """task 失敗時呼叫；不受 NOTIFY_MODE 影響、強制發。"""
    return notify(message, force=True)


def notify_success(message: str) -> bool:
    """task 成功時呼叫；error_only mode 會被略過。"""
    return notify(message, force=False)


def notify_with_buttons(message: str, buttons: list) -> bool:
    """
    發 Telegram 訊息含 inline_keyboard buttons（強制發、不受 error_only 影響）。
    buttons: list of list of {text, callback_data}
    e.g. [[{"text": "✅ 批准合併", "callback_data": "lib_merge_now"}, {"text": "❌ 略過", "callback_data": "lib_merge_skip"}]]

    **TG only**（2026-07-02 起）：Library 重複偵測通知只推 Telegram，不再推 LINE。
    重複合併按鈕操作走 TG，避免 LINE 端收到重複卡。
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    ok = False
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "reply_markup": {"inline_keyboard": buttons},
                },
                timeout=15,
            )
            if resp.status_code == 200:
                ok = True
            else:
                logger.warning(f"Telegram inline button API HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram inline button 通知例外：{e}")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定，跳過 TG 通知")

    return ok
