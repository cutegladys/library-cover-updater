"""
eBookReading 大 epub 補處理器（接 GAS 解不開、punt 到 _review 的大 epub）

背景：Kobo eBookReading/_inbox 的 epub 編目主路徑是 GAS Library/inboxProcessor.js
（processInboxScheduled 每日 16:00 + HTTP action processInbox）。但 GAS 的
`Utilities.unzip` 對 >~50MB 的 epub 解不開 → `_ipParseEpubMeta` 回 null → 檔案被
punt 進 `eBookReading/_inbox/_review`（Drive），編不了目卡在那。Python 的 zipfile
沒有這個大小限制 → 本 task 用 zipfile 解析、把 _review 內的大 epub 補編目落根。

流程（對齊 inbox_processor，但來源是 _inbox/_review、只收 .epub）：
1. 列 eBookReading/_inbox/_review/ 下的 .epub
2. download → parse_epub_meta（zipfile，無 50MB 限制）
3. 簡轉繁（zhconv）→ normalize（對齊 GAS normalizer）→ 組 "[作者] 書名.epub"
4. 重複檢查（ALL title+author）
5. 抽內建封面 → 上傳 Cover Art → IMAGE 公式（無封面 fallback Drive thumbnail）
6. 寫 ALL（title/author/lang/source/status/U/V/封面）
7. 搬檔（_review → eBookReading 根、改名）；Python 仍解不開 → 留 _review + 通知

⚠ 雙跑互斥：
- GAS 只碰 _inbox 直層 epub（小檔），大檔由它 punt 進 _review、之後不再讀 _review。
- inbox_processor（Python）只碰 _inbox 直層 pdf。
- 本 task 只碰 _inbox/_review 的 .epub（GAS 已 punt 的大檔）。
- _review 內的 .pdf（inbox_processor punt 的截斷/壞檔）本 task 不碰（只收 .epub）。
三條路徑來源資料夾 + 副檔名都不重疊，不會重複處理同一檔。

env：
  COVER_UPDATER_DRY_RUN     1 = 只列檔、不寫表不移檔
  REVIEW_EPUB_PROCESS_LIMIT 單次處理上限（預設 50）
"""
import logging
import os
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.relay import relay_call
from utils.sheets import sheet_id
from utils.drive import upload_cover, safe_name, image_formula_for_drive_file
from tasks.inbox_processor import (
    EBOOK_ROOT_ID, INBOX_NAME, REVIEW_NAME, MAIN_SHEET,
    find_subfolder, download_to_bytes, parse_epub_meta,
    simp_to_trad_meta, derive_clean_name, is_duplicate, append_to_all,
)
from tasks.cover_drive import extract_epub_cover

logger = logging.getLogger("library_cover_updater.review_epub_processor")


def list_epubs_in_folder(drive_service, folder_id: str):
    """列出 folder 下所有 .epub（只收 epub；_review 內的 pdf 是別條路徑 punt 的、不碰）。"""
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
        lower = name.lower()
        if mt == "application/epub+zip" or lower.endswith(".epub"):
            epubs.append(f)
    return epubs


def render_and_upload_epub_cover(drive_service, epub_bytes: bytes, title: str):
    """抽 epub 內建封面 → 上傳 Cover Art folder → 回 IMAGE() 公式字串。
    沒有內建封面或失敗返 None（append_to_all 會 fallback 到 Drive thumbnail）。
    """
    try:
        img_bytes, ext = extract_epub_cover(epub_bytes)
        if not img_bytes:
            return None
        img_mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        cover_id = upload_cover(drive_service, safe_name(title) + f".{ext}", img_bytes, img_mime)
        return image_formula_for_drive_file(cover_id)
    except Exception as e:
        logger.warning(f"  抽 epub 封面失敗（fallback thumbnail）：{e}")
        return None


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    limit = int(os.environ.get("REVIEW_EPUB_PROCESS_LIMIT", "50"))

    creds = load_creds()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[review_epub_processor] 找 _inbox / _review 資料夾...")
    inbox_id = find_subfolder(drive_service, EBOOK_ROOT_ID, INBOX_NAME)
    if not inbox_id:
        logger.warning(f"[review_epub_processor] 找不到 _inbox 子資料夾 in {EBOOK_ROOT_ID}")
        return {"task": "review_epub_processor", "targets": 0, "note": "no_inbox_folder"}

    review_id = find_subfolder(drive_service, inbox_id, REVIEW_NAME)
    if not review_id:
        # 沒有 _review = GAS 從沒 punt 過大檔，正常無事可做
        return {"task": "review_epub_processor", "targets": 0, "note": "no_review_folder"}

    epubs = list_epubs_in_folder(drive_service, review_id)
    logger.info(f"[review_epub_processor] _review 下 epub={len(epubs)}（GAS 解不開 punt 來的大檔）")
    if not epubs:
        return {"task": "review_epub_processor", "targets": 0, "note": "no_review_epub"}

    epubs = epubs[:limit]

    # 讀 ALL 看重複 + 找尾列
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
    still_failed = []  # GAS 解不開、Python 也解不開 → 真壞檔

    for f in epubs:
        orig_name = f["name"]
        file_id = f["id"]
        logger.info(f"[review_epub_processor] 處理: {orig_name}")

        try:
            file_bytes = download_to_bytes(drive_service, file_id)
        except HttpError as e:
            logger.warning(f"  download err: {e}")
            stats["download_err"] += 1
            continue

        meta = parse_epub_meta(file_bytes)
        if not meta:
            # GAS 解不開（大小）+ Python zipfile 也解不開（真壞 / 加密 / 非 zip）→ 留 _review 通知
            logger.warning(f"  Python zipfile 也解不開 → 留 _review、列入通知")
            stats["still_unparseable"] += 1
            still_failed.append(orig_name)
            continue

        simp_to_trad_meta(meta)
        clean_title, clean_author, new_name = derive_clean_name(meta, ".epub", orig_name)

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

        # 寫表（非重複才寫）+ 抽封面
        if not is_dup:
            url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            cover_formula = render_and_upload_epub_cover(drive_service, file_bytes, clean_title)
            if cover_formula:
                stats["cover_extracted"] += 1
            else:
                stats["cover_fallback_thumbnail"] += 1
            try:
                current_last_row = append_to_all(sheets_service, sid, current_last_row, {
                    "title": clean_title,
                    "author": clean_author,
                    "lang": meta["lang"],
                    "url": url,
                    "file_id": file_id,
                    "cover_formula": cover_formula,
                })
                all_data.append([clean_title, clean_author])
                stats["written"] += 1
            except HttpError as e:
                logger.warning(f"  寫表 err: {e}")
                stats["write_err"] += 1
                continue

        # 搬檔：_review → eBookReading 根 + 改名（走 relay，SA 不能搬 owner 的檔）
        try:
            relay_call("drive.move", {
                "fileId": file_id,
                "addParent": EBOOK_ROOT_ID,
                "removeParent": review_id,
                "newName": new_name,
            })
            stats["moved"] += 1
        except Exception as e:
            logger.warning(f"  搬檔 err: {e}")
            stats["move_err"] += 1

    logger.info(f"[review_epub_processor] 完成 stats={dict(stats)}")

    # 真壞檔（GAS 解不開 + Python 也解不開）→ notify_error 強推（actionable，§七 rule 17）
    if still_failed and not dry_run:
        try:
            from utils.notify import notify_error
            notify_error(
                f"⚠️ review_epub_processor：{len(still_failed)} 個 epub GAS 與 Python 都解不開\n"
                f"仍留在 eBookReading/_inbox/_review/，請人工檢查（可能損毀/加密/非標準 epub）：\n"
                + "\n".join(f"  • {n}" for n in still_failed[:10])
            )
        except Exception as e:
            logger.warning(f"still_unparseable 通知失敗：{e}")

    return {
        "task": "review_epub_processor",
        "targets": len(epubs),
        "elapsed_sec": 0,
        "stats": dict(stats),
    }
