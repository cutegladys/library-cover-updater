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

from utils.oauth import load_creds
from utils.relay import relay_call
from utils.sheets import sheet_id
from utils.normalizer import normalize_filename, normalize_author, build_filename
from utils.drive import upload_cover, safe_name, image_formula_for_drive_file
from tasks.cover_drive import render_pdf_first_page

logger = logging.getLogger("library_cover_updater.inbox_processor")

EBOOK_ROOT_ID = "1N-CIlms9t6HHyui52682bAC-gzVmZH3H"
INBOX_NAME = "_inbox"
REVIEW_NAME = "_review"  # _inbox 下的子資料夾、放解析失敗的 epub
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


def get_or_create_subfolder(drive_service, parent_id: str, name: str) -> str:
    """找 parent 下指定名稱的子資料夾、找不到就建（建夾走 relay，SA 不能建）。回 fileId。"""
    fid = find_subfolder(drive_service, parent_id, name)
    if fid:
        return fid
    res = relay_call("drive.create_folder", {"parentId": parent_id, "name": name})
    return res["folderId"]


def list_books_in_folder(drive_service, folder_id: str):
    """列出 folder 下所有 .pdf（含對應 mime 與副檔名）。

    分工：epub 走 GAS Library/inboxProcessor.js（0 UrlFetch、已穩定）；
    pdf 走 Python（PyMuPDF metadata + 封面渲染一次到位）。
    epub 在 Python 端不處理（防止雙跑）。
    每筆額外帶 `book_kind`，目前永遠是 'pdf'，主迴圈仍 keep 分流欄保留擴充空間。
    """
    r = drive_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType, webViewLink, parents)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    books = []
    for f in r.get("files", []):
        name = f.get("name", "")
        mt = f.get("mimeType", "")
        lower = name.lower()
        if mt == "application/pdf" or lower.endswith(".pdf"):
            f["book_kind"] = "pdf"
            books.append(f)
    return books


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


def is_pdf_truncated(pdf_bytes: bytes) -> bool:
    """檢查 PDF 末尾 2KB 有沒有 %%EOF / startxref。

    截斷的 PDF（下載中斷）兩個結構都會缺；PyMuPDF 開檔可能仍部分讀得到 metadata，
    但 render 第 1 頁通常會炸；最好提早攔下、通知 user 重下。
    """
    if len(pdf_bytes) < 100:
        return True  # 太小直接視為壞
    tail = pdf_bytes[-2048:]
    s = tail.decode("latin-1", errors="ignore")
    return ("%%EOF" not in s) and ("startxref" not in s)


def parse_pdf_meta(pdf_bytes: bytes, fallback_filename: str = ""):
    """解析 PDF metadata → title / author / lang。失敗返 None。

    1. PyMuPDF doc.metadata：title / author / subject / keywords / language
    2. metadata 空白時從檔名 fallback（去掉副檔名與括號中段）
    3. 結尾 title 仍空 → None（讓主迴圈搬 _review）
    """
    import fitz
    title = ""
    author = ""
    lang = ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            md = doc.metadata or {}
            title = (md.get("title") or "").strip()
            author = (md.get("author") or "").strip()
            # PDF 沒有標準 language 欄；偶爾在 Lang 屬性
            lang = ""
        finally:
            doc.close()
    except Exception as e:
        logger.warning(f"  parse_pdf_meta PyMuPDF err: {e}")
        # 不直接 return None — 還有檔名 fallback 可以救

    if not title and fallback_filename:
        # 從檔名抽：去 .pdf、去結尾的 ()、把 _ - 變空格
        base = re.sub(r"\.pdf$", "", fallback_filename, flags=re.I)
        base = re.sub(r"\s*\([^)]*\)\s*$", "", base).strip()
        if base:
            title = base

    if not title:
        return None

    return {
        "title": title,
        "author": author,
        "lang": lang,
        "series": "",
        "series_index": "",
    }


def render_and_upload_pdf_cover(drive_service, pdf_bytes: bytes, title: str):
    """PDF 第 1 頁 render → 上傳到 Cover Art folder → 回 IMAGE() 公式字串。
    失敗返 None（主迴圈會 fallback 到 Drive thumbnail）。
    """
    try:
        png = render_pdf_first_page(pdf_bytes)
        if not png:
            return None
        cover_id = upload_cover(drive_service, safe_name(title) + ".png", png, "image/png")
        return image_formula_for_drive_file(cover_id)
    except Exception as e:
        logger.warning(f"  render_and_upload_pdf_cover err: {e}")
        return None


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
    """append 一列到 ALL。注意 Sheets API 用 USER_ENTERED 寫 URL 會自動變超連結。

    info 可選 cover_formula：呼叫端已渲染好的封面公式（PDF 用），有就直接用，
    沒有就退回 Drive thumbnail（epub 用，後續會被 cover_drive 改寫成真封面）。
    """
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
    cover_formula = info.get("cover_formula") or \
        f'=IMAGE("https://drive.google.com/thumbnail?id={info["file_id"]}&sz=w400")'
    updates.append({"range": f"'{MAIN_SHEET}'!Q{new_row}", "values": [[cover_formula]]})

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    return new_row


def move_file_to_folder(drive_service, file_id: str, new_parent_id: str, old_parent_id: str, new_name: str):
    """改名 + 從 _inbox 搬到 eBookReading 根目錄（搬檔+改名走 relay，SA 不能搬 owner 的檔）。"""
    relay_call("drive.move", {
        "fileId": file_id,
        "addParent": new_parent_id,
        "removeParent": old_parent_id,
        "newName": new_name,
    })


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    limit = int(os.environ.get("INBOX_PROCESS_LIMIT", "50"))

    creds = load_creds()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[inbox_processor] 找 _inbox 資料夾...")
    inbox_id = find_subfolder(drive_service, EBOOK_ROOT_ID, INBOX_NAME)
    if not inbox_id:
        logger.warning(f"[inbox_processor] 找不到 _inbox 子資料夾 in {EBOOK_ROOT_ID}")
        return {"task": "inbox_processor", "targets": 0, "note": "no_inbox_folder"}

    # _review 子資料夾（_inbox 下、放解析失敗的 epub）— dry_run 也建（idempotent）
    review_id = get_or_create_subfolder(drive_service, inbox_id, REVIEW_NAME) if not dry_run else find_subfolder(drive_service, inbox_id, REVIEW_NAME)

    books = list_books_in_folder(drive_service, inbox_id)
    # 過濾掉 _review 資料夾下的檔案（list_books_in_folder 只列 inbox 直接子層，但保險加 parents 過濾）
    books = [f for f in books if review_id is None or review_id not in (f.get("parents") or [])]
    n_pdf = sum(1 for f in books if f["book_kind"] == "pdf")
    logger.info(f"[inbox_processor] _inbox 下 pdf={n_pdf} (epub 由 GAS 處理、Python 端跳過)")

    if not books:
        return {"task": "inbox_processor", "targets": 0, "note": "no_new_books"}

    books = books[:limit]

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

    for f in books:
        orig_name = f["name"]
        file_id = f["id"]
        kind = f["book_kind"]  # 'epub' or 'pdf'
        logger.info(f"[inbox_processor] 處理: {orig_name} ({kind})")

        # Download + parse
        try:
            file_bytes = download_to_bytes(drive_service, file_id)
        except HttpError as e:
            logger.warning(f"  download err: {e}")
            stats["download_err"] += 1
            continue

        if kind == "pdf":
            # 預檢：截斷 PDF（下載中斷）直接搬 _review、不浪費後續處理
            if is_pdf_truncated(file_bytes):
                logger.warning(f"  ⚠️ PDF 截斷（檔尾無 %%EOF/startxref）→ 移入 _review")
                stats["pdf_truncated"] += 1
                if not dry_run and review_id:
                    try:
                        relay_call("drive.move", {
                            "fileId": file_id,
                            "addParent": review_id,
                            "removeParent": inbox_id,
                        })
                        stats["moved_to_review"] += 1
                    except Exception as e:
                        logger.warning(f"  搬到 _review err: {e}")
                        stats["move_review_err"] += 1
                continue
            meta = parse_pdf_meta(file_bytes, fallback_filename=orig_name)
        else:
            meta = parse_epub_meta(file_bytes)
        if not meta:
            logger.warning(f"  metadata 解析失敗 → 移入 _review")
            stats["parse_failed"] += 1
            if not dry_run and review_id:
                try:
                    relay_call("drive.move", {
                        "fileId": file_id,
                        "addParent": review_id,
                        "removeParent": inbox_id,
                    })
                    stats["moved_to_review"] += 1
                except Exception as e:
                    logger.warning(f"  搬到 _review err: {e}")
                    stats["move_review_err"] += 1
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
        ext = ".pdf" if kind == "pdf" else ".epub"
        new_name = build_filename(clean_title, clean_author, ext)

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
            # PDF：當場 render 封面（cover_drive 只認 mime=application/pdf；新檔走完一次就齊全）
            cover_formula = None
            if kind == "pdf":
                cover_formula = render_and_upload_pdf_cover(drive_service, file_bytes, clean_title)
                if cover_formula:
                    stats["pdf_cover_rendered"] += 1
                else:
                    stats["pdf_cover_failed"] += 1
            try:
                current_last_row = append_to_all(sheets_service, sid, current_last_row, {
                    "title": clean_title,
                    "author": clean_author,
                    "lang": meta["lang"],
                    "url": url,
                    "file_id": file_id,
                    "cover_formula": cover_formula,
                })
                # 加進 all_data 避免下一個 epub 重複比對
                all_data.append([clean_title, clean_author])
                stats["written"] += 1
            except HttpError as e:
                logger.warning(f"  寫表 err: {e}")
                stats["write_err"] += 1
                continue

        # 搬檔（走 relay，失敗為 RuntimeError 非 HttpError → 廣捕）
        try:
            move_file_to_folder(drive_service, file_id, EBOOK_ROOT_ID, inbox_id, new_name)
            stats["moved"] += 1
        except Exception as e:
            logger.warning(f"  搬檔 err: {e}")
            stats["move_err"] += 1

    logger.info(f"[inbox_processor] 完成 stats={dict(stats)}")

    # 截斷的 PDF 走 notify_error 強推（user 才會看到）
    if stats.get("pdf_truncated", 0) > 0 and not dry_run:
        try:
            from utils.notify import notify_error
            notify_error(
                f"⚠️ inbox_processor 偵測到 {stats['pdf_truncated']} 個截斷的 PDF（檔尾無 %%EOF）\n"
                f"已搬到 eBookReading/_inbox/_review/，請重新下載完整版後再丟回 _inbox"
            )
        except Exception as e:
            logger.warning(f"truncated PDF 通知失敗：{e}")

    return {
        "task": "inbox_processor",
        "targets": len(books),
        "elapsed_sec": 0,
        "stats": dict(stats),
    }
