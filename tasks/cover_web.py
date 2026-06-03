"""
Library 封面修補 — 網路搜尋來源（Apple Books / Google Books / OpenLibrary）

掃 ALL 分頁 U 欄**無 fileId**（沒 Drive 檔）+ Q 欄空 + (Marker 空 OR NO_COVER) 的列：
  1. Apple Books search → 100x100 → 替換成 600x600
  2. Google Books API → http→https
  3. OpenLibrary cover by cover_i ID

成功：寫 Q 欄 IMAGE 公式 + 清 Marker（如有 NO_COVER）
失敗：寫 Marker=NO_COVER

對應 fetchBookCovers.js 的 fetchBookCovers + refetchBookCoversWithoutCover 兩支函式合一。

env：
  MAX_ROWS_PER_RUN      預設 200
  COVER_UPDATER_DRY_RUN 1 = 只印不寫
  WEB_SEARCH_RATE_HZ    每秒最多幾次（預設 5；保護 Apple Books 較嚴）
"""
import logging
import os
import time
import urllib.parse
from collections import Counter

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.sheets import (
    COL_TITLE, COL_COVER, COL_MARKER, COL_DRIVE_LINK,
    extract_file_id, classify_cover,
    sheet_id, read_all_rows, clear_cell,
)

logger = logging.getLogger("library_cover_updater.cover_web")

MARKER_NO_COVER = "NO_COVER"
COL_AUTHOR = 1  # B 欄
HTTP_TIMEOUT = 10


# ── 三層搜尋（移植自 fetchBookCovers.js）─────────────────────────

def search_apple_books(title: str, author: str):
    try:
        q = urllib.parse.quote(f"{title} {author or ''}")
        url = f"https://itunes.apple.com/search?term={q}&media=ebook&entity=ebook&limit=1"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("resultCount", 0) > 0:
            artwork = data["results"][0].get("artworkUrl100")
            if artwork:
                return artwork.replace("100x100", "600x600")
    except Exception as e:
        logger.debug(f"Apple search err: {e}")
    return None


def search_google_books(title: str, author: str):
    try:
        q = urllib.parse.quote(f"{title} {author or ''}")
        url = f"https://www.googleapis.com/books/v1/volumes?q={q}&maxResults=1"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        items = data.get("items", [])
        if items and items[0].get("volumeInfo", {}).get("imageLinks"):
            thumb = items[0]["volumeInfo"]["imageLinks"].get("thumbnail")
            if thumb:
                return thumb.replace("http://", "https://")
    except Exception as e:
        logger.debug(f"Google search err: {e}")
    return None


def search_openlibrary(title: str, author: str):
    try:
        url = (
            "https://openlibrary.org/search.json"
            f"?title={urllib.parse.quote(title)}"
            f"&author={urllib.parse.quote(author or '')}"
            "&limit=1"
        )
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("numFound", 0) > 0:
            cover_id = data["docs"][0].get("cover_i")
            if cover_id:
                return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
    except Exception as e:
        logger.debug(f"OpenLib search err: {e}")
    return None


def search_web(title: str, author: str):
    """三層搜尋，回 (image_url, source) or (None, None)。"""
    url = search_apple_books(title, author)
    if url:
        return url, "Apple"
    url = search_google_books(title, author)
    if url:
        return url, "Google"
    url = search_openlibrary(title, author)
    if url:
        return url, "OpenLib"
    return None, None


# ── target 識別 + 寫入 ────────────────────────────────────────

def find_targets(sheets_service):
    """
    找需要走網路搜尋的列：
      - 有書名
      - Q 欄空
      - U 欄**無** fileId（有的話讓 cover_drive 處理）
      - Marker 空 或 NO_COVER（NO_COVER 算 refetch）
    """
    rows = read_all_rows(sheets_service)
    targets = []
    for idx, row in enumerate(rows, start=2):
        while len(row) <= COL_DRIVE_LINK:
            row.append("")
        title = str(row[COL_TITLE]).strip()
        if not title:
            continue
        author = str(row[COL_AUTHOR]).strip() if len(row) > COL_AUTHOR else ""
        marker = str(row[COL_MARKER]).strip()
        if marker not in ("", MARKER_NO_COVER):
            continue
        # U 欄有 fileId → 跳過給 cover_drive
        fid = extract_file_id(str(row[COL_DRIVE_LINK]))
        if fid:
            continue
        # Q 欄已有任何內容 → 跳過（避免覆蓋）
        cover_class = classify_cover(str(row[COL_COVER]))
        if cover_class != "empty":
            continue
        targets.append((idx, title, author, marker))
    return targets


def write_cover_formula(sheets_service, row_num: int, image_url: str, dry_run: bool):
    """Q 欄寫 IMAGE 公式（image_url 來自網路）。注意：不改 F 欄 Source。"""
    if dry_run:
        return
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id(),
        range=f"'ALL'!Q{row_num}",
        valueInputOption="USER_ENTERED",
        body={"values": [[f'=IMAGE("{image_url}")']]},
    ).execute()


def write_marker_no_cover(sheets_service, row_num: int, dry_run: bool):
    if dry_run:
        return
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id(),
        range=f"'ALL'!T{row_num}",
        valueInputOption="USER_ENTERED",
        body={"values": [[MARKER_NO_COVER]]},
    ).execute()


# ── 主流程 ─────────────────────────────────────────────────────

def run():
    max_rows = int(os.environ.get("MAX_ROWS_PER_RUN", "200"))
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    rate_hz = float(os.environ.get("WEB_SEARCH_RATE_HZ", "5"))
    sleep_per = max(0.0, 1.0 / max(rate_hz, 0.1))

    creds = load_creds()
    sheets_service = build("sheets", "v4", credentials=creds)

    logger.info("[cover_web] Loading Library sheet...")
    targets = find_targets(sheets_service)
    logger.info(f"[cover_web] Targets: {len(targets)} (本次最多 {max_rows} 筆，dry_run={dry_run})")

    if not targets:
        return {"task": "cover_web", "targets": 0, "note": "no_new_books"}

    targets = targets[:max_rows]
    stats = Counter()
    start = time.time()

    for i, (row_num, title, author, marker) in enumerate(targets, 1):
        image_url, source = search_web(title, author)
        if image_url:
            try:
                write_cover_formula(sheets_service, row_num, image_url, dry_run)
                if marker == MARKER_NO_COVER and not dry_run:
                    # 找到了，清掉 NO_COVER marker
                    clear_cell(sheets_service, f"'ALL'!T{row_num}", dry_run=dry_run)
                stats[f"found_{source.lower()}"] += 1
                if marker == MARKER_NO_COVER:
                    stats["refetch_recovered"] += 1
            except HttpError as e:
                stats[f"write_err_{e.resp.status}"] += 1
                logger.warning(f"[cover_web] row {row_num} write err {e.resp.status}: {e}")
        else:
            try:
                write_marker_no_cover(sheets_service, row_num, dry_run)
                stats["no_cover_marked"] += 1
            except HttpError as e:
                stats[f"marker_err_{e.resp.status}"] += 1
                logger.warning(f"[cover_web] row {row_num} marker err {e.resp.status}: {e}")

        if i % 20 == 0:
            elapsed = time.time() - start
            logger.info(f"[cover_web]   進度 {i}/{len(targets)}  elapsed={elapsed:.0f}s  stats={dict(stats)}")
        time.sleep(sleep_per)

    elapsed = time.time() - start
    logger.info(f"[cover_web] 完成 elapsed={elapsed:.0f}s stats={dict(stats)}")
    return {
        "task": "cover_web",
        "targets": len(targets),
        "elapsed_sec": int(elapsed),
        "stats": dict(stats),
    }
