"""
Library 封面修補 — Drive 檔來源（PyMuPDF / EPUB / image / thumbnail）

掃 ALL 分頁 U 欄有 fileId + Q 欄非 Drive 圖 + Marker 非 DRIVE_GONE/NO_COVER 的列：
  PDF  → PyMuPDF 渲染首頁 2x PNG → 上傳 Cover Art → IMAGE 公式
  EPUB → 解 zip 抓內建封面 → 同上
  image → 直接 uc?export=view
  其他（audio/video）→ Drive thumbnailLink fallback

設計上限 MAX_ROWS_PER_RUN（預設 200）避免單次跑太久。
新書一週 5-20 本完全夠用。

env：
  MAX_ROWS_PER_RUN      預設 200
  COVER_UPDATER_DRY_RUN 1 = 只印不寫（除錯）
"""
import io
import logging
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import (
    COL_TITLE, COL_SOURCE, COL_COVER, COL_MARKER, COL_DRIVE_LINK,
    extract_file_id, classify_cover,
    read_all_rows, write_q_and_source, mark_drive_gone,
)
from utils.drive import (
    get_metadata, download_media, upload_cover, safe_name,
    image_formula_for_drive_file, image_formula_for_thumbnail,
)

logger = logging.getLogger("library_cover_updater.cover_drive")


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


def find_targets(sheets_service):
    """掃 sheet 找 U 欄有 fileId + Q 欄非 Drive 圖 + Marker 正常的列。"""
    rows = read_all_rows(sheets_service)
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
    return targets


def process_one(drive_service, sheets_service, row_num, fid, title, dry_run):
    """處理一列。返 (cover_class or None, error_str or None)。"""
    try:
        meta = get_metadata(drive_service, fid)
    except HttpError as e:
        if e.resp.status == 404:
            mark_drive_gone(sheets_service, row_num, dry_run=dry_run)
            return ("dead_404_marked", None)
        return (None, f"meta_err_{e.resp.status}")

    if meta.get("trashed"):
        return ("trashed", None)

    mime = meta.get("mimeType", "")
    try:
        size = int(meta.get("size", "0"))
    except (TypeError, ValueError):
        size = 0

    formula = None
    cover_class = None

    try:
        if mime == "application/pdf" and size <= 200 * 1024 * 1024:
            pdf_bytes = download_media(drive_service, fid)
            png = render_pdf_first_page(pdf_bytes)
            if png:
                cover_id = upload_cover(drive_service, safe_name(title) + ".png", png, "image/png")
                formula = image_formula_for_drive_file(cover_id)
                cover_class = "pdf_rendered"
        elif mime == "application/epub+zip" and size <= 100 * 1024 * 1024:
            epub_bytes = download_media(drive_service, fid)
            img_bytes, ext = extract_epub_cover(epub_bytes)
            if img_bytes:
                img_mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                cover_id = upload_cover(drive_service, safe_name(title) + f".{ext}", img_bytes, img_mime)
                formula = image_formula_for_drive_file(cover_id)
                cover_class = "epub_extracted"
        elif mime.startswith("image/"):
            formula = image_formula_for_drive_file(fid)
            cover_class = "image_direct"

        if not formula and meta.get("thumbnailLink"):
            formula = image_formula_for_thumbnail(meta["thumbnailLink"])
            if not cover_class:
                cover_class = f"thumbnail_{mime.split('/')[0] if mime else 'unknown'}"
    except Exception as e:
        logger.warning(f"row {row_num} fid={fid} mime={mime} EXC: {e}")
        if meta.get("thumbnailLink"):
            formula = image_formula_for_thumbnail(meta["thumbnailLink"])
            cover_class = "thumbnail_fallback_exc"
        else:
            return (None, "process_exception")

    if not formula:
        return ("no_formula", None)

    try:
        write_q_and_source(sheets_service, row_num, formula, dry_run=dry_run)
        return (cover_class, None)
    except HttpError as e:
        return (None, f"write_err_{e.resp.status}")


def run():
    """task entry point。返 stats dict 給 main.py 統計。"""
    max_rows = int(os.environ.get("MAX_ROWS_PER_RUN", "200"))
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    logger.info("[cover_drive] Loading Library sheet...")
    targets = find_targets(sheets_service)
    logger.info(f"[cover_drive] Targets: {len(targets)} (本次最多 {max_rows} 筆，dry_run={dry_run})")

    if not targets:
        return {"task": "cover_drive", "targets": 0, "note": "no_new_books"}

    targets = targets[:max_rows]
    stats = Counter()
    start = time.time()

    for i, (row_num, fid, title) in enumerate(targets, 1):
        cover_class, err = process_one(drive_service, sheets_service, row_num, fid, title, dry_run)
        if err:
            stats[err] += 1
        elif cover_class:
            stats[cover_class] += 1
        if i % 20 == 0:
            elapsed = time.time() - start
            logger.info(f"[cover_drive]   進度 {i}/{len(targets)}  elapsed={elapsed:.0f}s  stats={dict(stats)}")

    elapsed = time.time() - start
    logger.info(f"[cover_drive] 完成 elapsed={elapsed:.0f}s stats={dict(stats)}")
    return {
        "task": "cover_drive",
        "targets": len(targets),
        "elapsed_sec": int(elapsed),
        "stats": dict(stats),
    }
