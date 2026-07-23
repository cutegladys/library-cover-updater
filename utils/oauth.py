"""共用 Google 身分。

自 2026-06-03 起固定採混合身分：
- Service Account 讀 Drive／Sheets。
- 需要 owner 身分的建檔、搬檔、建夾與丟垃圾桶走 ``utils.relay``。

過渡期的 user OAuth rollback 已於 2026-07 移除。讀取身分只有
``GOOGLE_SA_JSON`` 一條路徑；缺少時必須 fail closed，不能靜默退回會到期的 token。
"""
import json
import os

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


def load_sa_creds(raw=None):
    """載入 Service Account credentials。

    正式 runtime 從 ``GOOGLE_SA_JSON`` 讀取；一次性工具可把已在記憶體中的同一值
    明確傳入，避免再實作第二套 credential parser。
    """
    if raw is None:
        raw = os.environ.get("GOOGLE_SA_JSON", "")
    raw = raw.strip()
    if not raw:
        raise RuntimeError("缺少 env：GOOGLE_SA_JSON")
    info = _parse_sa_json(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def load_creds():
    """所有 task 的唯一讀取身分入口。"""
    return load_sa_creds()
