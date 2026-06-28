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

    LINE：若設了 LINE_CHANNEL_ACCESS_TOKEN + LINE_PUSH_USER_ID（Library 頻道），額外推一張
    Flex 按鈕卡（best-effort，與 TG 同款按鈕）。merge_book/force_merge_book/ignore_book 的 postback
    由 Library GAS lineWebhook 通用合成 callback_query 路由到既有 merge handler；候選資料在
    _DupCandidates 分頁（全域、不分平台），合併結果經 guard 回 LINE。未設 env 則安全略過。
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

    # LINE leg（best-effort，不影響 TG 結果）
    _push_line_flex_buttons(message, buttons)
    return ok


def _push_line_flex_buttons(message: str, buttons: list) -> bool:
    """把 message + inline_keyboard 轉成 LINE Flex 按鈕卡並 push 到 Library LINE 頻道。
    需 env LINE_CHANNEL_ACCESS_TOKEN + LINE_PUSH_USER_ID；未設則安全略過（回 False）。
    """
    line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_uid = os.environ.get("LINE_PUSH_USER_ID", "").strip()
    if not line_token or not line_uid:
        return False

    # inline_keyboard（list of rows of {text, callback_data}）→ LINE Flex button 元件（postback）
    btn_components = []
    for row in (buttons or []):
        for b in (row or []):
            text = str(b.get("text", "")).strip()
            data = str(b.get("callback_data", "")).strip()
            if not text or not data:
                continue
            is_ignore = ("忽略" in text) or data.startswith("ignore_")
            btn_components.append({
                "type": "button",
                "style": "secondary" if is_ignore else "primary",
                "color": None if is_ignore else "#5B7FBF",
                "height": "sm",
                "action": {
                    "type": "postback",
                    "label": text[:20],          # LINE label 上限 20
                    "data": data[:300],          # LINE postback data 上限 300
                    "displayText": text[:20],
                },
            })
    # 去掉 color=None（LINE 不接受 null）
    for c in btn_components:
        if c.get("color") is None:
            c.pop("color", None)

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#5B7FBF", "paddingAll": "md",
            "contents": [{"type": "text", "text": "Library 重複偵測", "weight": "bold",
                          "color": "#FFFFFF", "size": "md", "wrap": True}],
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg",
            "contents": [{"type": "text", "text": str(message)[:1500], "size": "sm",
                          "color": "#333333", "wrap": True}],
        },
    }
    if btn_components:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm",
                            "contents": btn_components}

    flex_msg = {"type": "flex", "altText": str(message)[:380], "contents": bubble}
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {line_token}", "Content-Type": "application/json"},
            json={"to": line_uid, "messages": [flex_msg]},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        logger.warning(f"LINE push API HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"LINE Flex 按鈕通知例外：{e}")
        return False
