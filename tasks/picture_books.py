"""
英文繪本資源 Inbox 處理器 — 完整版（Phase 4-1）

對應 pictureBooksProcessor.js _pbFindOrphans + _pbWriteDraft。

流程：
1. recursive list root folder（英文讀本資源 1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx）下所有檔
2. 對比 ALL V 欄已知 fileId 找 orphans
3. 對每個 orphan 嘗試 guess title：
   - epub: 下載 + parse content.opf → dc:title / dc:creator
   - 其他: folder name 或檔名（去副檔名）
4. 簡轉繁
5. 寫 _PB_Draft 分頁（10 欄 schema 對齊原 GAS）
6. Telegram 通知

⚠️ 簡化點（已標 TODO）：
- 不用 Gemini API 智慧解析（原 GAS 也沒用、folder name 就夠）；若未來想啟用、加 GEMINI_API_KEY env + google-generativeai SDK

env:
  COVER_UPDATER_DRY_RUN  1 = 只列、不寫 sheet
  PB_PARSE_EPUB_META     1 = 對 .epub orphan 下載 + parse content.opf 補 title/author（耗時、預設 false）
"""
import io
import logging
import os
import re
import time
import zipfile
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from utils.oauth import load_creds
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.picture_books")

PICTUREBOOKS_ROOT_ID = "1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx"
MAIN_SHEET = "ALL"
DRAFT_SHEET = "_PB_Draft"

# 對應 GAS _PB
SOURCE_VALUE = "Google Drive"
STATUS_VALUE = "已擁有"


def list_files_recursive(drive_service, root_id: str):
    result = []
    stack = [(root_id, "", "")]
    visited = set()

    while stack:
        folder_id, parent_path, folder_name = stack.pop()
        if folder_id in visited:
            continue
        visited.add(folder_id)

        current_path = parent_path + ("/" + folder_name if folder_name else "")

        page_token = None
        while True:
            try:
                r = drive_service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
            except HttpError as e:
                logger.warning(f"  list err in {folder_id}: {e}")
                break

            for f in r.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    stack.append((f["id"], current_path, f["name"]))
                else:
                    result.append({
                        "id": f["id"],
                        "name": f["name"],
                        "mime": f["mimeType"],
                        "url": f.get("webViewLink", f"https://drive.google.com/file/d/{f['id']}/view"),
                        "path": current_path,
                    })

            page_token = r.get("nextPageToken")
            if not page_token:
                break

    return result


def ensure_draft_sheet(sheets_service, spreadsheet_id, dry_run):
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == DRAFT_SHEET:
            return
    if dry_run:
        return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": DRAFT_SHEET}}}]},
    ).execute()


def parse_epub_meta_safe(epub_bytes):
    """從 epub bytes 嘗試抽 title / author，失敗返 (None, None)。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(epub_bytes))
    except zipfile.BadZipFile:
        return None, None

    opf_path = None
    try:
        container = z.read("META-INF/container.xml").decode("utf-8", errors="replace")
        m = re.search(r'full-path="([^"]+\.opf)"', container, re.I)
        if m:
            opf_path = m.group(1)
    except Exception:
        pass
    if not opf_path:
        opfs = [n for n in z.namelist() if n.lower().endswith(".opf")]
        if not opfs:
            return None, None
        opf_path = opfs[0]

    try:
        opf = z.read(opf_path).decode("utf-8", errors="replace")
        t_m = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", opf, re.I | re.S)
        a_m = re.search(r"<dc:creator[^>]*>([^<]+)</dc:creator>", opf, re.I | re.S)
        title = t_m.group(1).strip() if t_m else None
        author = a_m.group(1).strip() if a_m else None
        return title, author
    except Exception:
        return None, None


def guess_title_author(drive_service, orphan: dict, parse_epub: bool):
    """
    對 orphan 猜書名/作者。
    - 如 PB_PARSE_EPUB_META=true 且 mime=epub → 下載 + parse content.opf
    - fallback: folder name 或檔名（去副檔名）
    """
    title = None
    author = ""

    if parse_epub and orphan["mime"] == "application/epub+zip":
        try:
            request = drive_service.files().get_media(fileId=orphan["id"], supportsAllDrives=True)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            title, author = parse_epub_meta_safe(buf.getvalue())
        except Exception as e:
            logger.warning(f"  epub parse err {orphan['id']}: {e}")

    if not title:
        # folder name fallback
        folder_name = orphan["path"].rstrip("/").split("/")[-1] if orphan["path"] else ""
        if folder_name:
            title = folder_name
        else:
            # 檔名去副檔名
            title = re.sub(r"\.[^.]+$", "", orphan["name"])

    return title or "", author or ""


def to_traditional(text: str) -> str:
    if not text:
        return text
    try:
        from zhconv import convert
        return convert(text, "zh-tw")
    except Exception:
        return text


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    parse_epub = os.environ.get("PB_PARSE_EPUB_META", "").lower() in ("1", "true", "yes")

    creds = load_creds()
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info(f"[picture_books] recursive list {PICTUREBOOKS_ROOT_ID} (parse_epub={parse_epub})...")
    start = time.time()
    files = list_files_recursive(drive_service, PICTUREBOOKS_ROOT_ID)
    logger.info(f"[picture_books] 共找到 {len(files)} 個檔案 ({time.time()-start:.0f}s)")

    if not files:
        return {"task": "picture_books", "targets": 0, "note": "no_files"}

    # 比對 ALL V 欄
    v_res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{MAIN_SHEET}'!V2:V",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    known_fids = set()
    for r in v_res.get("values", []):
        if r and r[0]:
            known_fids.add(str(r[0]).strip())
    logger.info(f"[picture_books] ALL V 欄已有 fileId: {len(known_fids)}")

    # 對齊 GAS _pbFindOrphans line 187-188：排除 _duplicates_quarantine 隔離資料夾
    # （dedup 工具放的重複檔、非真實新繪本、不該算 orphan）
    orphans = [
        f for f in files
        if f["id"] not in known_fids
        and "_duplicates_quarantine" not in f.get("path", "")
        and "_duplicates_quarantine" not in f.get("name", "")
    ]
    quarantine_skipped = sum(
        1 for f in files
        if f["id"] not in known_fids
        and ("_duplicates_quarantine" in f.get("path", "") or "_duplicates_quarantine" in f.get("name", ""))
    )
    logger.info(f"[picture_books] Orphans: {len(orphans)}（已排除 _duplicates_quarantine {quarantine_skipped} 個）")

    if not orphans:
        return {"task": "picture_books", "targets": 0, "stats": {"orphans": 0}, "note": "no_orphans"}

    # 對每個 orphan guess title/author + 簡轉繁
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ensure_draft_sheet(sheets_service, sid, dry_run)

    # _PB_Draft schema（對齊 GAS）：
    # A 書名(草稿) | B 作者(草稿) | C 語言 | D 來源 | E 狀態 | F FileId | G Drive URL | H 資料夾路徑 | I 建立時間 | J 操作
    rows = [["書名(草稿)", "作者(草稿)", "語言", "來源", "狀態", "FileId", "Drive URL", "資料夾路徑", "建立時間", "操作"]]

    s2t_changed = 0
    for o in orphans:
        title, author = guess_title_author(drive_service, o, parse_epub)
        orig_title = title
        title = to_traditional(title)
        author = to_traditional(author)
        if orig_title != title:
            s2t_changed += 1
        rows.append([
            title[:100],
            author[:80],
            "英文",  # 預設語言
            SOURCE_VALUE,
            STATUS_VALUE,
            o["id"],
            o["url"],
            o["path"][:200],
            ts,
            "PENDING",
        ])

    if s2t_changed > 0:
        logger.info(f"[picture_books] 🔁 簡→繁：{s2t_changed}/{len(orphans)} 筆 title 有變動")

    # ⚠️ Reconcile 邏輯（對齊 _Screenshot_Draft / 原 _pbWriteDraft，保留 user 標記 + 自動清理已處理）
    # 規則：
    #   既有 in 當前 orphan set → 保留（user 可能還在 review）
    #   既有 NOT in 當前 set + 操作 ∈ (APPROVED, DONE, REJECTED) → 刪（已處理）
    #   既有 NOT in 當前 set + PENDING (or 空) → 保留（user 還沒看完、安全網）
    #   新 orphan NOT in 既有 → append
    existing_data = []  # list of (row_num, file_id, action)
    try:
        existing_res = sheets_service.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{DRAFT_SHEET}'!A1:J",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        existing_vals = existing_res.get("values", [])
        # row index 從 2 起（Row 1 是 header）；如分頁無 header 則從 1
        if existing_vals:
            has_header_row = str(existing_vals[0][0] if len(existing_vals[0]) > 0 else "").strip() in ("書名(草稿)", "FileId", "BookTitle")
            start_idx = 1 if has_header_row else 0
            for i, r in enumerate(existing_vals[start_idx:], start=start_idx + 1):
                while len(r) < 10:
                    r.append("")
                fid = str(r[5]).strip()
                action = str(r[9]).strip().upper()
                if fid:
                    existing_data.append((i, fid, action))
    except HttpError as e:
        if e.resp.status != 400:
            raise

    existing_fid_set = {fid for _, fid, _ in existing_data}
    current_orphan_fids = {o["id"] for o in orphans}
    logger.info(f"[picture_books] _PB_Draft 既有 {len(existing_data)} 列、當前 orphan {len(current_orphan_fids)} 個")

    # 找要刪的列（既有 NOT in 當前 + action 已處理）
    DONE_ACTIONS = {"APPROVED", "DONE", "REJECTED", "COMMITTED"}
    rows_to_delete = [row_num for row_num, fid, action in existing_data
                      if fid not in current_orphan_fids and action in DONE_ACTIONS]
    rows_to_delete.sort(reverse=True)  # 從大 row_num 開始刪（避免 index shift）

    # 找要 append 的：新 orphan NOT in 既有 fid
    new_rows = [r for r in rows[1:] if str(r[5]).strip() not in existing_fid_set]
    has_header = bool(existing_data) or (existing_res.get("values", []) and len(existing_res.get("values", [])[0]) > 0)
    if not has_header:
        new_rows = [rows[0]] + new_rows  # 寫 header

    logger.info(f"[picture_books] 計畫：append {len(new_rows)} 列、刪 {len(rows_to_delete)} 列已處理（剩餘 PENDING/未標記 保留）")

    if dry_run:
        return {"task": "picture_books", "targets": len(orphans), "stats": {
            "orphans": len(orphans), "append_planned": len(new_rows),
            "delete_planned": len(rows_to_delete), "s2t_changed": s2t_changed, "dry_run": True,
        }}

    # 1. 刪已處理列（用 batchUpdate deleteDimension）
    if rows_to_delete:
        # 拿 _PB_Draft 的 sheetId
        meta = sheets_service.spreadsheets().get(spreadsheetId=sid).execute()
        pb_sheet_id = next((s["properties"]["sheetId"] for s in meta["sheets"]
                            if s["properties"]["title"] == DRAFT_SHEET), None)
        if pb_sheet_id is not None:
            delete_requests = [
                {"deleteDimension": {"range": {
                    "sheetId": pb_sheet_id, "dimension": "ROWS",
                    "startIndex": r - 1, "endIndex": r,
                }}} for r in rows_to_delete
            ]
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": delete_requests},
            ).execute()

    # 2. Append 新 orphan
    if new_rows:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"'{DRAFT_SHEET}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    stats = {
        "orphans": len(orphans),
        "appended": len(new_rows),
        "deleted_done": len(rows_to_delete),
        "kept_existing": len(existing_data) - len(rows_to_delete),
        "s2t_changed": s2t_changed,
    }
    logger.info(f"[picture_books] 完成 stats={stats}")
    return {
        "task": "picture_books",
        "targets": len(orphans),
        "elapsed_sec": int(time.time() - start),
        "stats": stats,
    }
