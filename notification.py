"""Telegram 直連通知（不依賴 GAS / 其他服務）。"""
import os
import logging
import requests

logger = logging.getLogger("library_cover_updater")


def notify(message: str) -> bool:
    """直接呼叫 Telegram Bot API 發訊息。失敗不 raise，只 log。"""
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
