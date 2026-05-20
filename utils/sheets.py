"""共用 Library 試算表常數與 helpers。"""
import os
import re

# Library 試算表 column index（0-based）
COL_TITLE = 0       # A
COL_SOURCE = 5      # F
COL_COVER = 16      # Q
COL_MARKER = 19     # T
COL_DRIVE_LINK = 20 # U

# Q 欄當前 IMAGE 公式 URL host 分類用
NETWORK_HOSTS = (
    "mzstatic.com", "books.google.com", "books.googleusercontent.com",
    "openlibrary.org", "covers.openlibrary", "amazon.com",
    "images-amazon", "media-amazon",
)


def sheet_id() -> str:
    sid = os.environ.get("LIBRARY_SHEET_ID", "").strip()
    if not sid:
        raise RuntimeError("缺少 env：LIBRARY_SHEET_ID")
    return sid


def extract_file_id(s):
    """從 Drive URL / HYPERLINK 抽 fileId。"""
    if not s:
        return None
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    return None


def classify_cover(q):
    """Q 欄當前狀態分類：empty / drive / network / other_image。"""
    if not q:
        return "empty"
    if "drive.google.com" in q:
        return "drive"
    for h in NETWORK_HOSTS:
        if h in q:
            return "network"
    return "other_image"


def read_all_rows(sheets_service):
    """讀 ALL 分頁 A2:U 全部、回傳含 formula 的 rows。"""
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id(),
        range="'ALL'!A2:U",
        valueRenderOption="FORMULA",
    ).execute()
    return res.get("values", [])


def write_q_and_source(sheets_service, row_num: int, formula: str, dry_run: bool = False):
    """Q 欄寫公式 + F 欄改 Google Drive（雙重保險）。dry_run 不寫。"""
    if dry_run:
        return
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id(),
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": f"'ALL'!Q{row_num}", "values": [[formula]]},
                {"range": f"'ALL'!F{row_num}", "values": [["Google Drive"]]},
            ],
        },
    ).execute()


def mark_drive_gone(sheets_service, row_num: int, dry_run: bool = False):
    """T 欄標 DRIVE_GONE（檔案真 404 時使用）。dry_run 不寫。"""
    if dry_run:
        return
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id(),
        range=f"'ALL'!T{row_num}",
        valueInputOption="USER_ENTERED",
        body={"values": [["DRIVE_GONE"]]},
    ).execute()


def clear_cell(sheets_service, range_a1: str, dry_run: bool = False):
    """清掉某個 cell（例如 marker = NO_COVER 找到網路封面後清掉）。"""
    if dry_run:
        return
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=sheet_id(),
        range=range_a1,
    ).execute()
