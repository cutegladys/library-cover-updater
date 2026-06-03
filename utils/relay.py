"""呼叫 MarukoRestrictedRelay 做 owner-身分的 Drive 寫操作。

SA 讀、relay 寫 的混合身分：Service Account 沒 My Drive 配額、也不能搬/丟 owner
擁有的檔，所以「建檔 / 搬檔(+改名) / 建夾 / 丟垃圾桶」這幾類擁有者動作，
都以使用者身分經 MarukoRestrictedRelay 執行（relay 住免驗證預設專案、token 不過期）。

env：
  RELAY_URL    relay web app /exec URL（未設用內建常數）
  RELAY_TOKEN  relay token（= ClaudeAPI CLAUDE_API_TOKEN 同值）
"""
import os

import requests

# 非機密；換 relay deployment 時改這裡或設 env RELAY_URL。
RELAY_URL_DEFAULT = (
    "https://script.google.com/macros/s/"
    "AKfycbx_iLUGn6L7_LMVDoHjy77AE8THAxgeX-CQt3_5pi0F9gIQeNxNo0LlhQzaQdtvW3yjNQ/exec"
)


def _relay_url() -> str:
    return os.environ.get("RELAY_URL", "").strip() or RELAY_URL_DEFAULT


def _relay_token() -> str:
    t = os.environ.get("RELAY_TOKEN", "").strip()
    if not t:
        raise RuntimeError("缺少 env：RELAY_TOKEN")
    return t


def relay_call(action: str, params: dict = None, timeout: int = 120) -> dict:
    """POST {token, clientName, action, params} 到 relay；回 result dict／raise on error。

    GAS web app 會 302 redirect 到 googleusercontent，requests 預設 follow（轉 GET）拿到 JSON。
    """
    body = {
        "token": _relay_token(),
        "clientName": "library-cover-updater",
        "action": action,
        "params": params or {},
    }
    resp = requests.post(
        _relay_url(),
        json=body,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"relay {action} HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"relay {action} 回傳非 JSON：{resp.text[:300]}")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"relay {action} error：{data['error']}")
    return data
