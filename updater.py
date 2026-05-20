"""
Library 封面修補主邏輯（single-run, Zeabur 容器排程觸發）

掃 Library 試算表「U 欄有 fileId + Q 欄非 Drive 圖 + Marker 非 DRIVE_GONE/NO_COVER」：
  PDF  → PyMuPDF 渲染首頁 2x PNG → 上傳 Cover Art → IMAGE 公式
  EPUB → 解 zip 抓內建封面 → 同上
  image → 直接 uc?export=view
  其他（audio/video）→ Drive thumbnailLink fallback
同時把 F 欄 Source 改成 "Google Drive" 雙重保險。

設計上限 MAX_ROWS_PER_RUN（預設 200），避免單次跑太久。
新書一週 5-20 本完全夠用；歷史 backlog 由本機 stage2_process.py 一次性跑完（2026-05-20 完成）。
"""

import io
import json
import os
import re
import time
import zipfile
import logging
import xml.etree.ElementTree as ET
from collections import Counter

logger = logging.getLogger("library_cover_updater")


COL_TITLE = 0
COL_SOURCE = 5
COL_COVER = 16
COL_MARKER = 19
COL_DRIVE_LINK = 20

NETWORK_HOSTS = (
    "mzstatic.com", "books.google.com", "books.googleusercontent.com",
    "openlibrary.org", "covers.openlibrary", "amazon.com",
    "images-amazon", "media-amazon",
)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def extract_file_id(s):
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
    if not q:
        return "empty"
    if "drive.google.com" in q:
        return "drive"
    for h in NETWORK_HOSTS:
        if h in q:
            return "network"
    return "other_image"


def render_pdf_first_page(pdf_bytes):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count == 0:
            return None
        page = doc[0]
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def extract_epub_cover(epub_bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(epub_bytes))
    except zipfile.BadZipFile:
        return None, None
    opf_path = None
    try:
        container = z.read("META-INF/container.xml")
        root = ET.fromstring(container)
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        rf = root.find(".//c:rootfile", ns)
        if rf is not None:
            opf_path = rf.attrib.get("full-path")
    except Exception:
        pass
    if not opf_path:
        opfs = [n for n in z.namelist() if n.lower().endswith(".opf")]
        if not opfs:
            return None, None
        opf_path = opfs[0]
    try:
        opf_bytes = z.read(opf_path)
        opf_root = ET.fromstring(opf_bytes)
        ns2 = {"o": "http://www.idpf.org/2007/opf"}
        cover_href = None
        for item in opf_root.findall(".//o:item", ns2):
            if "cover-image" in item.attrib.get("properties", ""):
                cover_href = item.attrib.get("href")
                break
        if not cover_href:
            for meta in opf_root.findall(".//o:meta", ns2):
                if meta.attrib.get("name", "").lower() == "cover":
                    cid = meta.attrib.get("content")
                    if cid:
                        for item in opf_root.findall(".//o:item", ns2):
                            if item.attrib.get("id") == cid:
                                cover_href = item.attrib.get("href")
                                break
                    break
        if not cover_href:
            for item in opf_root.findall(".//o:item", ns2):
                mt = item.attrib.get("media-type", "")
                href = item.attrib.get("href", "")
                if mt.startswith("image/") and "cover" in href.lower():
                    cover_href = href
                    break
        if not cover_href:
            return None, None
        opf_dir = os.path.dirname(opf_path)
        full = (opf_dir + "/" + cover_href) if opf_dir else cover_href
        full = full.replace("\\", "/").lstrip("/")
        if full not in z.namelist():
            lower = full.lower()
            cand = [n for n in z.namelist() if n.lower() == lower]
            if cand:
                full = cand[0]
            else:
                return None, None
        img_bytes = z.read(full)
        ext = os.path.splitext(full)[1].lstrip(".").lower() or "jpg"
        return img_bytes, ext
    except Exception:
        return None, None


_safe_re = re.compile(r"[^\w\-. ]+", re.UNICODE)


def safe_name(s, max_len=80):
    s = _safe_re.sub("_", s).strip()
    return s[:max_len] or "cover"


def run_cover_update():
    """掃描並修補封面，回傳 stats dict。"""
    # ── env 檢查 ────────────────────────────────────────────────
    token_raw = os.environ.get("GOOGLE_USER_TOKEN_JSON", "").strip()
    sheet_id = os.environ.get("LIBRARY_SHEET_ID", "").strip()
    cover_folder = os.environ.get("COVER_ART_FOLDER_ID", "").strip()
    max_rows = int(os.environ.get("MAX_ROWS_PER_RUN", "200"))
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    missing = [k for k, v in [
        ("GOOGLE_USER_TOKEN_JSON", token_raw),
        ("LIBRARY_SHEET_ID", sheet_id),
        ("COVER_ART_FOLDER_ID", cover_folder),
    ] if not v]
    if missing:
        raise RuntimeError(f"缺少必填 env：{missing}")

    # ── lazy imports ────────────────────────────────────────────
    import fitz  # noqa: F401
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    from googleapiclient.errors import HttpError
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    # ── OAuth ───────────────────────────────────────────────────
    try:
        info = json.loads(token_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GOOGLE_USER_TOKEN_JSON 不是合法 JSON：{e}") from e
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    sheets = build("sheets", "v4", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    # ── 識別目標 ────────────────────────────────────────────────
    logger.info("Loading Library sheet...")
    res = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="'ALL'!A2:U",
        valueRenderOption="FORMULA",
    ).execute()
    rows = res.get("values", [])

    targets = []
    for idx, row in enumerate(rows, start=2):
        while len(row) <= COL_DRIVE_LINK:
            row.append("")
        title = str(row[COL_TITLE]).strip()
        if not title:
            continue
        marker = str(row[COL_MARKER]).strip()
        if marker in ("DRIVE_GONE", "NO_COVER"):
            continue
        fid = extract_file_id(str(row[COL_DRIVE_LINK]))
        if not fid:
            continue
        cover_class = classify_cover(str(row[COL_COVER]))
        if cover_class == "drive":
            continue
        targets.append((idx, fid, title))

    logger.info(f"Targets: {len(targets)} (本次最多 {max_rows} 筆，dry_run={dry_run})")

    if not targets:
        return {"targets": 0, "note": "no_new_books"}

    targets = targets[:max_rows]
    stats = Counter()
    start = time.time()

    def mark_drive_gone(row_num):
        if dry_run:
            return
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'ALL'!T{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [["DRIVE_GONE"]]},
        ).execute()

    def write_q_and_source(row_num, formula):
        if dry_run:
            return
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"'ALL'!Q{row_num}", "values": [[formula]]},
                    {"range": f"'ALL'!F{row_num}", "values": [["Google Drive"]]},
                ],
            },
        ).execute()

    def upload_cover(name, content_bytes, mime):
        media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime, resumable=False)
        f = drive.files().create(
            body={"name": name, "parents": [cover_folder]},
            media_body=media, fields="id", supportsAllDrives=True,
        ).execute()
        cover_id = f["id"]
        try:
            drive.permissions().create(
                fileId=cover_id, body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
        except HttpError:
            pass
        return cover_id

    for i, (row_num, fid, title) in enumerate(targets, 1):
        try:
            meta = drive.files().get(
                fileId=fid,
                fields="id,name,mimeType,thumbnailLink,trashed,size",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                mark_drive_gone(row_num)
                stats["dead_404_marked"] += 1
            else:
                stats[f"meta_err_{e.resp.status}"] += 1
                logger.warning(f"row {row_num} meta err {e.resp.status}: {e}")
            continue

        if meta.get("trashed"):
            stats["trashed"] += 1
            continue

        mime = meta.get("mimeType", "")
        try:
            size = int(meta.get("size", "0"))
        except (TypeError, ValueError):
            size = 0

        formula = None
        cover_class = None

        try:
            if mime == "application/pdf" and size <= 200 * 1024 * 1024:
                pdf_bytes = drive.files().get_media(fileId=fid, supportsAllDrives=True).execute()
                png = render_pdf_first_page(pdf_bytes)
                if png:
                    cover_id = upload_cover(safe_name(title) + ".png", png, "image/png")
                    formula = f'=IMAGE("https://drive.google.com/uc?export=view&id={cover_id}")'
                    cover_class = "pdf_rendered"
            elif mime == "application/epub+zip" and size <= 100 * 1024 * 1024:
                epub_bytes = drive.files().get_media(fileId=fid, supportsAllDrives=True).execute()
                img_bytes, ext = extract_epub_cover(epub_bytes)
                if img_bytes:
                    img_mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                    cover_id = upload_cover(safe_name(title) + f".{ext}", img_bytes, img_mime)
                    formula = f'=IMAGE("https://drive.google.com/uc?export=view&id={cover_id}")'
                    cover_class = "epub_extracted"
            elif mime.startswith("image/"):
                formula = f'=IMAGE("https://drive.google.com/uc?export=view&id={fid}")'
                cover_class = "image_direct"

            if not formula and meta.get("thumbnailLink"):
                thumb = meta["thumbnailLink"].replace('"', "")
                formula = f'=IMAGE("{thumb}")'
                if not cover_class:
                    cover_class = f"thumbnail_{mime.split('/')[0] if mime else 'unknown'}"
        except Exception as e:
            stats["process_exception"] += 1
            logger.warning(f"row {row_num} fid={fid} mime={mime} EXC: {e}")
            if meta.get("thumbnailLink"):
                thumb = meta["thumbnailLink"].replace('"', "")
                formula = f'=IMAGE("{thumb}")'
                cover_class = "thumbnail_fallback_exc"

        if not formula:
            stats["no_formula"] += 1
            continue

        try:
            write_q_and_source(row_num, formula)
            stats[cover_class] += 1
        except HttpError as e:
            stats[f"write_err_{e.resp.status}"] += 1
            logger.warning(f"row {row_num} write err {e.resp.status}: {e}")

        if i % 20 == 0:
            elapsed = time.time() - start
            logger.info(f"  進度 {i}/{len(targets)}  elapsed={elapsed:.0f}s  stats={dict(stats)}")

    elapsed = time.time() - start
    return {
        "targets": len(targets),
        "elapsed_sec": int(elapsed),
        "stats": dict(stats),
    }
