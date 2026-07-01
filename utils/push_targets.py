"""中央推播控制台讀取 helper（Python 版；對齊 jp bot / fitness / GAS pushTargets.js）。

來源：GladysMemo 試算表 PushTargets 分頁（欄序 Bot|TypeKey|說明|目標|備註），
目標 ∈ both|tg|line|off。走 ClaudeAPI sheet.read（跑在 owner=Gladys 帳號，免分享 SA）。

token：讀 CLAUDE_API_TOKEN；本服務未設此 env 時退回 RELAY_TOKEN（兩者同值，見 relay.py），
故不需為了接控制台在 Zeabur 另設新 env。

設計（fail-safe，絕不亂噴／漏推）：
- 讀表失敗 / 無 token / 查不到 row → 回 fallback（呼叫點傳「現況平台」），
  確保中央表不可用時不改變既有行為。
- 程序內快取 5 分鐘，避免每次判斷都打 GAS。
- 只控「主動推播」目標平台（本服務的重複偵測通知）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

_SHEET_ID = "1Z33wzgTMCkasAM6TcZ__0aPy1-p2rJmK3lw3FNlBpyQ"  # GladysMemo
_TAB = "PushTargets"
# ClaudeAPI Web App（跑在 Gladys 帳號＝Sheet owner，免分享 SA）；同 jp/fitness push_targets.py。
_CLAUDE_API_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbw9xug8rB5lu242Yx-zgo-BFjosjFEiVUgaYiPr7ZIGULboZVsmknl5vBGrLrJyF_3MGQ/exec"
)
_CACHE_SEC = 300

_cache: dict | None = None
_cache_ts = 0.0


def _token() -> str:
    return (os.environ.get("CLAUDE_API_TOKEN", "").strip()
            or os.environ.get("RELAY_TOKEN", "").strip())


def _load_map() -> dict | None:
    global _cache, _cache_ts
    now = time.time()
    if _cache is not None and (now - _cache_ts) < _CACHE_SEC:
        return _cache
    token = _token()
    if not token:
        return None  # 無 token → fallback（不改變既有行為）
    try:
        body = json.dumps({
            "token": token,
            "action": "sheet.read",
            "params": {"spreadsheetId": _SHEET_ID, "sheetName": _TAB, "maxRows": 500},
        }).encode("utf-8")
        req = urllib.request.Request(
            _CLAUDE_API_URL, data=body, headers={"Content-Type": "application/json"}
        )
        # GAS Web App POST 會 302 → urllib 預設跟隨改 GET 取結果（memory gas_curl_post_302_trap）。
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
        rows = res.get("rows", []) if isinstance(res, dict) else []
        m: dict[str, str] = {}
        for row in rows[1:]:
            if len(row) < 4:
                continue
            bot = str(row[0]).strip().lower()
            key = str(row[1]).strip()
            tgt = str(row[3]).strip().lower()
            if bot and key:
                m[bot + "::" + key] = tgt
        _cache = m
        _cache_ts = now
        return m
    except Exception:
        return None  # 讀表失敗 → fallback


def get_push_target(bot: str, key: str, fallback: str = "both") -> str:
    m = _load_map()
    if not m:
        return fallback
    v = m.get(bot.lower() + "::" + key)
    if v in ("both", "tg", "line", "off"):
        return v
    return fallback


def push_target_allows(target: str, platform: str) -> bool:
    if target == "off":
        return False
    if target == "both":
        return True
    if target == "tg":
        return platform == "telegram"
    if target == "line":
        return platform == "line"
    return True  # 未知值保守允許（配合 fallback 已是現況平台）


def allows(bot: str, key: str, platform: str, fallback: str = "both") -> bool:
    """便捷：該 bot/key 是否允許推到 platform（'telegram' | 'line'）。"""
    return push_target_allows(get_push_target(bot, key, fallback), platform)
