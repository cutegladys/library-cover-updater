"""共用 Google 身分。

混合身分（2026-06-03 起）：
- 目標：用 **Service Account**（讀 Drive/Sheets、token 不過期）+ relay 寫。
- 「建檔/搬檔/建夾/丟垃圾桶」等擁有者動作走 `utils/relay.py`（MarukoRestrictedRelay）。
- **過渡期 fail-safe**：`load_creds()` 預設仍走舊 **user OAuth**（避免 SA env / 分享尚未
  就緒就 auto-deploy 上線把 production 炸掉）；設 env `USE_SA_CREDS=1` 才切到 SA。
- Step 6 穩定後：把預設改成 SA、移除 user OAuth（`load_user_creds` + `GOOGLE_USER_TOKEN_JSON`）
  與這個開關。

背景：user OAuth client 掛標準專案 linecalendarbot-475101、同意畫面 Testing 模式
→ refresh token 每 7 天被 revoke、要手動瀏覽器重簽。SA + relay 根治。
"""
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _parse_sa_json(raw: str) -> dict:
    """接受原始 JSON 或 base64(JSON)。

    Zeabur CLI `variable update -k` 用 CSV parser、對含逗號/引號的原始 JSON 會 parse 失敗，
    故 GOOGLE_SA_JSON 建議存 base64（無逗號引號、CLI/.env/panel 都安全）。兩種都吃。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import base64
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"GOOGLE_SA_JSON 既非合法 JSON 也非合法 base64：{e}") from e
        return json.loads(decoded)


def load_sa_creds():
    """從 env GOOGLE_SA_JSON 載入 Service Account credentials（token 不過期）。"""
    raw = os.environ.get("GOOGLE_SA_JSON", "").strip()
    if not raw:
        raise RuntimeError("缺少 env：GOOGLE_SA_JSON")
    info = _parse_sa_json(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def load_user_creds():
    """[rollback 用] 從 env GOOGLE_USER_TOKEN_JSON 載入 OAuth credentials；自動 refresh。"""
    token_raw = os.environ.get("GOOGLE_USER_TOKEN_JSON", "").strip()
    if not token_raw:
        raise RuntimeError("缺少 env：GOOGLE_USER_TOKEN_JSON")
    try:
        info = json.loads(token_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GOOGLE_USER_TOKEN_JSON 不是合法 JSON：{e}") from e
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def load_creds():
    """所有 task build() 用此入口。

    過渡期 fail-safe：預設仍用舊 user OAuth；設 env USE_SA_CREDS=1 才切到 SA
    （Step 5 測試 / 正式切換）。Step 6 穩定後預設改 SA、移除此開關與 user OAuth。
    """
    if os.environ.get("USE_SA_CREDS", "").lower() in ("1", "true", "yes"):
        return load_sa_creds()
    return load_user_creds()
