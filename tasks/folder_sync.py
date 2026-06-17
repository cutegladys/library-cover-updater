"""
Library 檔名同步 — 用 Drive fileId 查當前檔名、對齊 sheet A 欄

對應 syncFolderNamesToSheet (listDriveFiles.js)。

流程：
1. 讀 ALL 分頁所有列
2. 對 Source=Google Drive + U 欄有 fileId 的列：
   - drive_service.files().get(fileId).name → 當前 Drive 檔名
   - 去副檔名跟 A 欄 title 比較
   - 不同 → 加入 update queue
3. batchUpdate 寫回 A 欄新檔名

對應原 GAS 版有 lock/stop/Telegram 互動、Python 容器無需（單 instance、cron 跑完即 idle）。
原版 6 分鐘 timeout 在 Python 容器無限制、一次跑完。

env：
  COVER_UPDATER_DRY_RUN     1 = 只印不寫
  FOLDER_SYNC_MAX_PER_RUN   單次最多檢查多少列（預設 0 = 不限制；過去 GAS 受 6 分鐘 timeout 才有此參數）
  FOLDER_SYNC_RETRY_TIMES   每筆 Drive API 失敗 retry 次數（預設 2、含原始呼叫共 3 次）
  FOLDER_SYNC_RETRY_DELAY   retry 之間 sleep 秒數（預設 1.5）

不可訪問的 fileId 會記入 inaccessibleFileIds set + Telegram 通知，
避免每天 cron 重複打到 dead/permission denied 檔案。
"""
import logging
import os
import random
import re
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.notify import notify_error
from utils.sheets import (
    COL_TITLE, COL_SOURCE, COL_NOTE, COL_MARKER, COL_DRIVE_LINK,
    extract_file_id, sheet_id, read_all_rows,
)

logger = logging.getLogger("library_cover_updater.folder_sync")

SOURCE_FILTER = "Google Drive"
EXT_RE = re.compile(r"\.[^.]*$")

# 🛡 分歧守門門檻：新檔名與「現有書名」和「原始檔名」字詞重疊都低於此值 → 視為 Drive 連結錯位、不覆寫
DIVERGENCE_OVERLAP_THRESHOLD = 0.34
# 🛡 同系列換集守門：非數字詞重疊 ≥ 此值且數字（集號/章號）不同 → 視為同系列換集錯位
SERIES_WORD_OVERLAP_THRESHOLD = 0.5
_ORIG_NAME_RE = re.compile(r"原始檔名[:：]\s*(.+)")
_WORD_RE = re.compile(r"[^0-9a-z一-鿿]+")
_ZLIB_RE = re.compile(r"\(z-?library\)", re.I)
_NUM_RE = re.compile(r"\d+")


def strip_ext(name: str) -> str:
    return EXT_RE.sub("", name).strip()


def extract_orig_stem(note: str) -> str:
    """從 I 欄備註抽『原始檔名』stem（去 doubled-note / Drive 註記分隔 + 去副檔名）。"""
    if not note:
        return ""
    m = _ORIG_NAME_RE.search(str(note))
    if not m:
        return ""
    raw = m.group(1).split("；")[0].split(" ; ")[0]
    return strip_ext(raw)


def _word_set(s: str) -> set:
    if not s:
        return set()
    x = _ZLIB_RE.sub(" ", str(s).lower())
    x = _WORD_RE.sub(" ", x)
    return {w for w in x.split() if w and not w.isdigit()}


def title_overlap(a: str, b: str) -> float:
    """|a 詞集 ∩ b 詞集| / |a 詞集|（a＝新檔名 為分母）。a 無可比字元時回 1（不誤擋）。"""
    sa, sb = _word_set(a), _word_set(b)
    if not sa:
        return 1.0
    return len(sa & sb) / len(sa)


def _num_set(s: str) -> set:
    """字串中所有整數 token（去前導零正規化，'09'→9、'030'→30）。"""
    return {int(n) for n in _NUM_RE.findall(str(s or ""))}


def is_series_volume_swap(new_name: str, ref: str) -> bool:
    """new_name 與 ref 同系列（非數字詞高度重疊）但集號/章號不同 → 疑似同系列換集錯位。

    這是 overlap 門檻的盲點補丁：同系列（如 Journey to the West ch30 vs ch67、
    The Last Firehawk #09 vs #07）共同字多、overlap 必然 ≥0.34 會被放行 →
    U 連結指到同系列別一集就把書名洗成別集。改用「非數字詞同系列 + 數字不同」精準偵測。
    ref 應傳真身（原始檔名 stem）；缺則 caller 傳現書名當 proxy。
    """
    nums_new, nums_ref = _num_set(new_name), _num_set(ref)
    # 兩邊都要有數字、且數字集合不同（同號＝同一集、非換集）
    if not nums_new or not nums_ref or nums_new == nums_ref:
        return False
    wn, wr = _word_set(new_name), _word_set(ref)
    if not wn or not wr:
        return False
    shared = len(wn & wr)
    if shared < 2:  # 至少 2 個共同非數字詞才算「同系列」（避免單字偶然命中）
        return False
    return shared / min(len(wn), len(wr)) >= SERIES_WORD_OVERLAP_THRESHOLD


def fetch_drive_meta_with_retry(drive_service, fid, retry_times: int, retry_delay: float):
    """打 drive.files().get、5xx / 429 自動 retry。回 (meta, last_err_status)。"""
    last_status = None
    for attempt in range(retry_times + 1):
        try:
            meta = drive_service.files().get(
                fileId=fid,
                fields="name,trashed",
                supportsAllDrives=True,
            ).execute()
            return meta, None
        except HttpError as e:
            last_status = e.resp.status
            # 4xx（非 429）：no retry — permission denied / 404 / 等都是固定錯誤
            if e.resp.status not in (429, 500, 502, 503, 504):
                return None, last_status
            if attempt < retry_times:
                # exponential backoff + jitter
                sleep_s = retry_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"  fileId={fid} status={e.resp.status} retry {attempt+1}/{retry_times} after {sleep_s:.1f}s")
                time.sleep(sleep_s)
    return None, last_status


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    max_per_run = int(os.environ.get("FOLDER_SYNC_MAX_PER_RUN", "0"))
    retry_times = int(os.environ.get("FOLDER_SYNC_RETRY_TIMES", "2"))
    retry_delay = float(os.environ.get("FOLDER_SYNC_RETRY_DELAY", "1.5"))

    creds = load_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    sid = sheet_id()

    logger.info("[folder_sync] Loading Library sheet...")
    rows = read_all_rows(sheets_service)
    logger.info(f"[folder_sync] Total rows: {len(rows)} (dry_run={dry_run})")

    # 收集 (row_num, fileId, title) tuples 要打 Drive API 查
    # 跳過 T 欄已標 DRIVE_GONE 的列（永久消失、不再重打）
    candidates = []
    skipped_drive_gone = 0
    for idx, row in enumerate(rows, start=2):
        while len(row) <= COL_DRIVE_LINK:
            row.append("")
        title = str(row[COL_TITLE]).strip()
        source = str(row[COL_SOURCE]).strip()
        marker = str(row[COL_MARKER]).strip()
        if source != SOURCE_FILTER:
            continue
        if not title:
            continue
        if marker == "DRIVE_GONE":
            skipped_drive_gone += 1
            continue
        fid = extract_file_id(str(row[COL_DRIVE_LINK]))
        if not fid:
            continue
        note_stem = extract_orig_stem(str(row[COL_NOTE]) if len(row) > COL_NOTE else "")
        candidates.append((idx, fid, title, note_stem))

    logger.info(f"[folder_sync] Candidates (Source=Google Drive + has fileId, marker!=DRIVE_GONE): {len(candidates)} (skipped DRIVE_GONE={skipped_drive_gone})")

    if max_per_run > 0:
        candidates = candidates[:max_per_run]
        logger.info(f"[folder_sync] limited to first {max_per_run} per run")

    if not candidates:
        return {"task": "folder_sync", "targets": 0, "note": "no_new_books"}

    # 對每個 candidate 查 Drive 當前檔名
    updates = []  # batchUpdate body
    stats = Counter()
    start = time.time()
    inaccessible_fids = []  # list of (row_num, fid, title, status)
    review_rows = []  # 🛡 分歧守門攔下、未覆寫的疑似錯位 (row_num, old_title, new_name, note_stem, fid)

    for i, (row_num, fid, title, note_stem) in enumerate(candidates, 1):
        meta, err_status = fetch_drive_meta_with_retry(drive_service, fid, retry_times, retry_delay)
        if meta is None:
            if err_status == 404:
                stats["dead_404"] += 1
                inaccessible_fids.append((row_num, fid, title, "404 not found"))
            elif err_status == 403:
                stats["dead_403"] += 1
                inaccessible_fids.append((row_num, fid, title, "403 permission denied"))
            else:
                stats[f"meta_err_{err_status}"] += 1
                if err_status:
                    inaccessible_fids.append((row_num, fid, title, f"err {err_status}"))
            continue

        if meta.get("trashed"):
            stats["trashed"] += 1
            inaccessible_fids.append((row_num, fid, title, "trashed"))
            continue

        current_name = meta.get("name", "")
        if not current_name:
            continue

        current_stripped = strip_ext(current_name)
        title_stripped = strip_ext(title)

        # 兩種比對方式（對應 GAS 邏輯）：去副檔名 或 原樣
        if current_stripped == title_stripped or current_stripped == title:
            stats["unchanged"] += 1
            continue

        # 🛡 分歧守門：新檔名若與「現有書名」和「原始檔名」字詞重疊都 < 門檻，
        #   ＝極可能是 U 欄 Drive 連結錯位（指到別本書）。不覆寫書名，改記 _TitleSyncReview 供人工檢視。
        #   背景：2026-06-17 發現整段書名被錯位的 Drive 連結默默洗掉（debug-log 2026-06-17）。
        ov_title = title_overlap(current_stripped, title)
        ov_note = title_overlap(current_stripped, note_stem) if note_stem else 1.0
        if ov_title < DIVERGENCE_OVERLAP_THRESHOLD and ov_note < DIVERGENCE_OVERLAP_THRESHOLD:
            stats["diverged_held"] += 1
            review_rows.append((row_num, title, current_stripped, note_stem, fid))
            logger.warning(
                f"[folder_sync] 🛡 守門攔下 row {row_num}：「{title[:40]}」↛「{current_stripped[:40]}」"
                f" (overlap title={ov_title:.2f} note={ov_note:.2f}) → _TitleSyncReview"
            )
            continue

        # 🛡 同系列換集守門：overlap 高（會被上面放行）但「新檔名 vs 真身」是同系列不同集號
        #   ＝ U 連結指到同系列別一集（如 Journey to the West ch30↔ch67）。不覆寫，記 _TitleSyncReview。
        #   有原始檔名 → 拿真身比（精準）；缺則拿現書名當 proxy（保守）。
        swap_ref = note_stem if note_stem else title
        if is_series_volume_swap(current_stripped, swap_ref):
            stats["series_swap_held"] += 1
            review_rows.append((row_num, title, current_stripped, note_stem, fid))
            logger.warning(
                f"[folder_sync] 🛡 同系列換集守門攔下 row {row_num}：「{title[:40]}」↛「{current_stripped[:40]}」"
                f" (真身=「{swap_ref[:40]}」集號不同) → _TitleSyncReview"
            )
            continue

        # 需更新：寫回 A 欄為去副檔名後的當前 Drive 名
        updates.append({
            "range": f"'ALL'!A{row_num}",
            "values": [[current_stripped]],
        })
        stats["needs_update"] += 1

        if i % 100 == 0:
            elapsed = time.time() - start
            logger.info(f"[folder_sync]   進度 {i}/{len(candidates)}  elapsed={elapsed:.0f}s  stats={dict(stats)}")

    elapsed = time.time() - start
    logger.info(f"[folder_sync] 比對完成 elapsed={elapsed:.0f}s, 待更新 {len(updates)} 筆")

    # 把這次新發現不可訪問的列 T 欄打 DRIVE_GONE marker，下次 cron 自動跳過
    marker_updates = [
        {"range": f"'ALL'!T{row_num}", "values": [["DRIVE_GONE"]]}
        for row_num, _fid, _title, _reason in inaccessible_fids
    ]
    all_updates = updates + marker_updates

    # batchUpdate 一次寫回（dry-run 時跳過）
    if all_updates and not dry_run:
        # batchUpdate 上限 Sheets API 預設沒嚴格上限，但 1000+ 個 range 可能超 quota，分批
        BATCH = 500
        for i in range(0, len(all_updates), BATCH):
            chunk = all_updates[i:i + BATCH]
            try:
                sheets_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sid,
                    body={
                        "valueInputOption": "USER_ENTERED",
                        "data": chunk,
                    },
                ).execute()
                stats["written"] += len(chunk)
            except HttpError as e:
                stats[f"write_err_{e.resp.status}"] += 1
                logger.warning(f"[folder_sync] batchUpdate err {e.resp.status}: {e}")
    stats["marker_drive_gone_new"] = len(marker_updates)

    # 🛡 把守門攔下的疑似錯位寫進 _TitleSyncReview（append；dry-run 不寫）
    if review_rows and not dry_run:
        try:
            _append_title_sync_review(sheets_service, sid, review_rows)
        except Exception as e:
            logger.warning(f"[folder_sync] 寫入 _TitleSyncReview 失敗：{e}")

    elapsed = time.time() - start
    logger.info(f"[folder_sync] 完成 elapsed={elapsed:.0f}s stats={dict(stats)}")

    # 🛡 守門攔下通知：書名沒被改、但有疑似 Drive 連結錯位待人工檢視
    if review_rows:
        rlines = [f"🛡 folder_sync 分歧守門：攔下 {len(review_rows)} 筆疑似 Drive 連結錯位（書名已保留、未被覆蓋），見 _TitleSyncReview 分頁"]
        for row_num, old_title, new_name, _ns, _fid in review_rows[:15]:
            rlines.append(f"  row {row_num} | 保留「{old_title[:32]}」 ↛ 未套用「{new_name[:32]}」")
        if len(review_rows) > 15:
            rlines.append(f"  ...另 {len(review_rows) - 15} 筆")
        try:
            notify_error("\n".join(rlines))
        except Exception as e:
            logger.warning(f"  review notify err: {e}")

    # 不可訪問檔案通知：只在「這次新發現」時發一次（DRIVE_GONE 已標的列上面已跳過）
    if inaccessible_fids:
        lines = [f"⚠️ folder_sync：本次新發現 {len(inaccessible_fids)} 個 Drive 檔案無法訪問（404/403/trashed），已自動標 T=DRIVE_GONE，未來 cron 不再重打"]
        for row_num, fid, title, reason in inaccessible_fids[:20]:
            lines.append(f"  row {row_num} | {title[:40]} | {reason}")
        if len(inaccessible_fids) > 20:
            lines.append(f"  ...另 {len(inaccessible_fids) - 20} 筆")
        try:
            notify_error("\n".join(lines))
        except Exception as e:
            logger.warning(f"  inaccessible notify err: {e}")

    return {
        "task": "folder_sync",
        "targets": len(candidates),
        "elapsed_sec": int(elapsed),
        "stats": dict(stats),
        "inaccessible_count": len(inaccessible_fids),
        "review_held": len(review_rows),
    }


def _append_title_sync_review(sheets_service, sid, review_rows):
    """把守門攔下的疑似錯位 append 到 _TitleSyncReview 分頁（不存在則建立 + 寫表頭）。"""
    import datetime
    sheet_name = "_TitleSyncReview"
    meta = sheets_service.spreadsheets().get(spreadsheetId=sid).execute()
    exists = any(s["properties"]["title"] == sheet_name for s in meta.get("sheets", []))
    if not exists:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{sheet_name}'!A1",
            valueInputOption="RAW",
            body={"values": [["時間", "行號", "原書名(已保留)", "Drive連結檔名(未套用)", "原始檔名stem", "FileId"]]},
        ).execute()
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    body = {"values": [[stamp, rn, old, new, ns, fid] for (rn, old, new, ns, fid) in review_rows]}
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"'{sheet_name}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()
