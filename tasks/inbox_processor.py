"""
eBookReading Inbox 處理器 — 對應 inboxProcessor.js (382 行)

流程：
1. 列 Drive eBookReading/_inbox/ 下的 epub 檔
2. 解析 epub content.opf → dc:title / dc:creator / dc:language / calibre:series
3. normalizer 清洗 → 組 "[作者] 書名.epub"
4. 重複檢查（ALL 分頁書名+作者）
5. 寫入 ALL（title/author/lang/source/status/U/V）+ 自動填封面
6. 移檔（_inbox → eBookReading 根目錄、改名）

⚠️ 半夜寫的簡化版差異：
- 跳過簡轉繁（原 GAS 用 simplifiedToTraditionalBatch；Python 要 opencc 依賴、半夜先 skip）
- 跳過解析失敗時的 _review 子資料夾搬移（原 GAS 邏輯保留、Python 暫略過、解析失敗就只 log + 留 _inbox）
- 上述兩項都加 TODO，user 起來決定是否補

env:
  COVER_UPDATER_DRY_RUN  1 = 只列檔、不寫表不移檔
  INBOX_PROCESS_LIMIT    單次處理上限（預設 50）
"""
import io
import logging
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from utils.oauth import load_user_creds
from utils.sheets import sheet_id
from utils.normalizer import normalize_filename, normalize_author, build_filename

logger = logging.getLogger("library_cover_updater.inbox_processor")

EBOOK_ROOT_ID = "1N-CIlms9t6HHyui52682bAC-gzVmZH3H"
INBOX_NAME = "_inbox"
MAIN_SHEET = "ALL"

# 1-based columns
COL_TITLE = 1
COL_AUTHOR = 2
COL_LANG = 3
COL_SOURCE = 6
COL_STATUS = 7
COL_COVER = 17  # Q
COL_DRIVE_URL = 21  # U
COL_FILE_ID = 22  # V

SOURCE_VALUE = "Google Drive"
STATUS_VALUE = "已擁有"


def find_subfolder(drive_service, parent_id: str, name: str):
    """找 parent 下指定名稱的子資料夾。回 fileId 或 None。"""
    r = drive_service.files().list(
        q=f"'{parent_id}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = r.get("files", [])
    return files[0]["id"] if files else None


def list_epubs_in_folder(drive_service, folder_id: str):
    """列出 folder 下所有 .epub（含 mime=epub 跟副檔名 .epub）。"""
    r = drive_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType, webViewLink, parents)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    epubs = []
    for f in r.get("files", []):
        name = f.get("name", "")
        mt = f.get("mimeType", "")
        if mt == "application/epub+zip" or name.lower().endswith(".epub"):
            epubs.append(f)
    return epubs


def download_to_bytes(drive_service, file_id: str) -> bytes:
    """下載 Drive file 成 bytes。"""
    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def parse_epub_meta(epub_bytes: bytes):
    """解析 epub content.opf → title / author / lang / series / series_index。失敗返 None。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(epub_bytes))
    except zipfile.BadZipFile:
        return None

    opf_path = None
    # 找 .opf（從 container.xml）
    try:
        container = z.read("META-INF/container.xml")
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        root = ET.fromstring(container)
        rf = root.find(".//c:rootfile", ns)
        if rf is not None:
            opf_path = rf.attrib.get("full-path")
    except Exception:
        pass
    if not opf_path:
        opfs = [n for n in z.namelist() if n.lower().endswith(".opf")]
        if not opfs:
            return None
        opf_path = opfs[0]

    try:
        opf_bytes = z.read(opf_path).decode("utf-8", errors="replace")
    except Exception:
        return None

    # 用 regex 抽（跟 GAS 原版一樣，不必嚴格 namespace 處理）
    def m1(pat):
        m = re.search(pat, opf_bytes, re.I | re.S)
        return m.group(1).strip() if m else ""

    title = m1(r"<dc:title[^>]*>([^<]+)</dc:title>")
    author = m1(r"<dc:creator[^>]*>([^<]+)</dc:creator>")
    lang = m1(r"<dc:language[^>]*>([^<]+)</dc:language>").lower()

    series = m1(r'<meta[^>]+name="calibre:series"[^>]+content="([^"]+)"')
    if not series:
        series = m1(r'<meta[^>]+content="([^"]+)"[^>]+name="calibre:series"')
    series_index = m1(r'<meta[^>]+name="calibre:series_index"[^>]+content="([^"]+)"')
    if not series_index:
        series_index = m1(r'<meta[^>]+content="([^"]+)"[^>]+name="calibre:series_index"')

    # EPUB3 belongs-to-collection
    if not series:
        series = m1(r'<meta[^>]+property="belongs-to-collection"[^>]*>([^<]+)</meta>')

    if not title:
        return None

    return {
        "title": title,
        "author": author,
        "lang": lang,
        "series": series,
        "series_index": series_index,
    }


def is_duplicate(all_data, title: str, author: str) -> bool:
    """檢查 ALL 是否已有同書名+同作者。"""
    t = title.lower().strip()
    a = author.lower().strip()
    for row in all_data:
        if len(row) >= 2:
            rt = str(row[0]).lower().strip()
            ra = str(row[1]).lower().strip()
            if rt == t and ra == a:
                return True
    return False


def append_to_all(sheets_service, sid: str, last_row: int, info: dict):
    """append 一列到 ALL。注意 Sheets API 用 USER_ENTERED 寫 URL 會自動變超連結。"""
    new_row = last_row + 1
    # 一次寫多 cell 用 batchUpdate
    updates = [
        {"range": f"'{MAIN_SHEET}'!A{new_row}", "values": [[info["title"]]]},
        {"range": f"'{MAIN_SHEET}'!B{new_row}", "values": [[info["author"]]]},
        {"range": f"'{MAIN_SHEET}'!C{new_row}", "values": [[info["lang"]]]},
        {"range": f"'{MAIN_SHEET}'!F{new_row}", "values": [[SOURCE_VALUE]]},
        {"range": f"'{MAIN_SHEET}'!G{new_row}", "values": [[STATUS_VALUE]]},
        {"range": f"'{MAIN_SHEET}'!U{new_row}", "values": [[info["url"]]]},
        {"range": f"'{MAIN_SHEET}'!V{new_row}", "values": [[info["file_id"]]]},
    ]
    # 簡易封面：thumbnailLink-based formula（避免依賴 Drive metadata API）
    cover_formula = f'=IMAGE("https://drive.google.com/thumbnail?id={info["file_id"]}&sz=w400")'
    updates.append({"range": f"'{MAIN_SHEET}'!Q{new_row}", "values": [[cover_formula]]})

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    return new_row


def move_file_to_folder(drive_service, file_id: str, new_parent_id: str, old_parent_id: str, new_name: str):
    """改名 + 從 _inbox 搬到 eBookReading 根目錄。"""
    drive_service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parent_id,
        body={"name": new_name},
        supportsAllDrives=True,
    ).execute()


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    limit = int(os.environ.get("INBOX_PROCESS_LIMIT", "50"))

    creds = load_user_creds()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[inbox_processor] 找 _inbox 資料夾...")
    inbox_id = find_subfolder(drive_service, EBOOK_ROOT_ID, INBOX_NAME)
    if not inbox_id:
        logger.warning(f"[inbox_processor] 找不到 _inbox 子資料夾 in {EBOOK_ROOT_ID}")
        return {"task": "inbox_processor", "targets": 0, "note": "no_inbox_folder"}

    epubs = list_epubs_in_folder(drive_service, inbox_id)
    logger.info(f"[inbox_processor] _inbox 下 epub 數: {len(epubs)}")

    if not epubs:
        return {"task": "inbox_processor", "targets": 0, "note": "no_new_books"}

    epubs = epubs[:limit]

    # 讀 ALL 看重複
    all_res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{MAIN_SHEET}'!A2:B",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    all_data = all_res.get("values", [])
    last_row_res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{MAIN_SHEET}'!A:A",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    current_last_row = len(last_row_res.get("values", []))

    stats = Counter()

    for f in epubs:
        orig_name = f["name"]
        file_id = f["id"]
        logger.info(f"[inbox_processor] 處理: {orig_name}")

        # Download + parse
        try:
            epub_bytes = download_to_bytes(drive_service, file_id)
        except HttpError as e:
            logger.warning(f"  download err: {e}")
            stats["download_err"] += 1
            continue

        meta = parse_epub_meta(epub_bytes)
        if not meta:
            logger.warning(f"  metadata 解析失敗")
            stats["parse_failed"] += 1
            continue

        # 簡轉繁（用 zhconv 純 Python）
        try:
            from zhconv import convert as zh_convert
            if meta["title"]:
                orig_t = meta["title"]
                meta["title"] = zh_convert(orig_t, "zh-tw")
                if orig_t != meta["title"]:
                    logger.info(f"  🔁 title 簡→繁：{orig_t} → {meta['title']}")
            if meta["author"]:
                orig_a = meta["author"]
                meta["author"] = zh_convert(orig_a, "zh-tw")
                if orig_a != meta["author"]:
                    logger.info(f"  🔁 author 簡→繁：{orig_a} → {meta['author']}")
            if meta.get("series"):
                orig_s = meta["series"]
                meta["series"] = zh_convert(orig_s, "zh-tw")
                if orig_s != meta["series"]:
                    logger.info(f"  🔁 series 簡→繁：{orig_s} → {meta['series']}")
        except Exception as e:
            logger.warning(f"  簡轉繁失敗（保留原文）：{e}")

        raw_title = meta["title"]
        clean_author = normalize_author(meta["author"])

        # 系列拼接（GAS 邏輯）
        if meta["series"]:
            series_core = re.sub(r"\s+\d+(\.\d+)?$", "", meta["series"]).strip()
            already_has = series_core.lower() in raw_title.lower()
            if not already_has:
                idx_str = ""
                if meta["series_index"]:
                    try:
                        n = float(meta["series_index"])
                        if n == int(n):
                            idx_str = " " + str(int(n)).zfill(2)
                        else:
                            idx_str = " " + meta["series_index"]
                    except ValueError:
                        idx_str = " " + meta["series_index"]
                raw_title = f"{meta['series']}{idx_str} - {meta['title']}"

        clean_title = normalize_filename(raw_title)
        new_name = build_filename(clean_title, clean_author, ".epub")

        # 重複檢查
        is_dup = is_duplicate(all_data, clean_title, clean_author)
        if is_dup:
            logger.info(f"  重複（跳過寫表）: {clean_title}")
            stats["duplicate"] += 1
        else:
            logger.info(f"  新書: {clean_title} | {clean_author}")
            stats["new"] += 1

        if dry_run:
            logger.info(f"  [dry_run] 將改名 → {new_name}" + ("（重複，不寫表）" if is_dup else "（將新增至 ALL）"))
            continue

        # 寫表（非重複才寫）
        if not is_dup:
            url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            try:
                current_last_row = append_to_all(sheets_service, sid, current_last_row, {
                    "title": clean_title,
                    "author": clean_author,
                    "lang": meta["lang"],
                    "url": url,
                    "file_id": file_id,
                })
                # 加進 all_data 避免下一個 epub 重複比對
                all_data.append([clean_title, clean_author])
                stats["written"] += 1
            except HttpError as e:
                logger.warning(f"  寫表 err: {e}")
                stats["write_err"] += 1
                continue

        # 搬檔
        try:
            move_file_to_folder(drive_service, file_id, EBOOK_ROOT_ID, inbox_id, new_name)
            stats["moved"] += 1
        except HttpError as e:
            logger.warning(f"  搬檔 err: {e}")
            stats["move_err"] += 1

    logger.info(f"[inbox_processor] 完成 stats={dict(stats)}")
    return {
        "task": "inbox_processor",
        "targets": len(epubs),
        "elapsed_sec": 0,
        "stats": dict(stats),
    }
