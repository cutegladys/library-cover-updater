"""共用 OAuth：載入主帳號 refresh token 給所有 task 用。"""
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def load_user_creds():
    """從 env GOOGLE_USER_TOKEN_JSON 載入 OAuth credentials；自動 refresh 過期 token。"""
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
