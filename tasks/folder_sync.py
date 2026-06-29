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
from utils.notify import notify_error, notify_success
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


INDEX_SHEET = "檔案清單_Export"  # drive_index 重建的全書庫活檔索引（A=FileName B=FileId F=MD5）
_KEY_RE = re.compile(r"[^0-9a-z一-鿿]+")
_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # 0-byte 檔 md5（會假比對，排除）


def norm_key(s: str) -> str:
    """正規化檔名/書名為比對 key：去副檔名、轉小寫、只留英數＋中日韓。"""
    return _KEY_RE.sub("", EXT_RE.sub("", (s or "").lower()))


def load_backfill_index(sheets_service):
    """讀『檔案清單_Export』活檔索引 → (key2files, md52files)。

    - key2files: {norm_key(filename): [(fileId, filename), ...]}（檔名比對，舊有）
    - md52files: {md5: [(fileId, filename), ...]}（F 欄 MD5，byte 相同最可靠；F 欄缺則為空 dict）

    drive_index 任務每週重建此分頁（trashed=false 的全書庫活檔）。folder_sync 用它把
    『舊 fileId 死掉但檔案被重傳/搬動仍在書庫』的列救回（re-link），而非永久標 DRIVE_GONE。
    索引缺/讀失敗 → 回 ({}, {})（degrade 成舊行為，不回補）。F 欄尚未由新版 drive_index
    寫入時 md52files 為空 → 自動退回純檔名比對（向後相容）。
    """
    try:
        vals = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id(), range=f"'{INDEX_SHEET}'!A2:F",
        ).execute().get("values", [])
    except HttpError as e:
        logger.warning(f"[folder_sync] 讀活檔索引失敗、停用回補：{e}")
        return {}, {}
    key2files, md52files = {}, {}
    for r in vals:
        name = r[0] if len(r) > 0 else ""
        fid = r[1] if len(r) > 1 else ""
        md5 = r[5] if len(r) > 5 else ""
        if name and fid:
            key2files.setdefault(norm_key(name), []).append((fid, name))
        if fid and md5 and md5 != _EMPTY_MD5:
            md52files.setdefault(md5, []).append((fid, name))
    return key2files, md52files


def find_backfill_md5(md52files: dict, alive_fids: set, md5: str):
    """用 MD5 在活檔索引找『同一份檔（byte 相同）還活著的檔』。回 (new_fid, new_name) 或 None。

    byte 完全相同即同一本書（最可靠、免守門 overlap）。命中 fid 已被別 alive 列用 → 回 None。
    """
    if not md5 or md5 == _EMPTY_MD5 or not md52files:
        return None
    for fid, name in md52files.get(md5, []):
        if fid not in alive_fids:
            return fid, name
    return None


def find_backfill(key2files: dict, alive_fids: set, title: str, note_stem: str):
    """在活檔索引找『同一本書還活著的檔』。回 (new_fid, new_name) 或 None。

    用原始檔名(真身)優先、否則書名比對；套 folder_sync 同一把守門（詞重疊≥門檻＋非同系列換集）
    避免 re-link 到錯書（對齊 2026-06-17 書名洗錯教訓）。命中的 fid 已被別 alive 列用 → 回 None
    （避免製造重複列）。多命中 → 精準檔名→最高 overlap→fileId 字典序 確定性挑一個。
    """
    if not key2files:
        return None
    ref = note_stem or title
    ref_s = strip_ext(ref)
    cands = key2files.get(norm_key(ref_s), [])
    good = [
        (cfid, cnm) for cfid, cnm in cands
        if title_overlap(strip_ext(cnm), ref_s) >= DIVERGENCE_OVERLAP_THRESHOLD
        and not is_series_volume_swap(strip_ext(cnm), ref_s)
    ]
    if not good:
        return None
    good.sort(key=lambda x: (
        strip_ext(x[1]).lower() != ref_s.lower(),
        -title_overlap(strip_ext(x[1]), ref_s),
        x[0],
    ))
    new_fid, new_name = good[0]
    if new_fid in alive_fids:
        return None
    return new_fid, new_name


def fetch_drive_meta_with_retry(drive_service, fid, retry_times: int, retry_delay: float):
    """打 drive.files().get、5xx / 429 自動 retry。回 (meta, last_err_status)。"""
    last_status = None
    for attempt in range(retry_times + 1):
        try:
            meta = drive_service.files().get(
                fileId=fid,
                fields="name,trashed,md5Checksum",
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

    # 回補用：活檔索引（檔名 + md5）+ 目前已被別列使用的 fileId（避免回補製造重複列）
    key2files, md52files = load_backfill_index(sheets_service)
    alive_fids = set()
    for row in rows:
        while len(row) <= COL_DRIVE_LINK:
            row.append("")
        if str(row[COL_MARKER]).strip() == "DRIVE_GONE":
            continue
        afid = extract_file_id(str(row[COL_DRIVE_LINK]))
        if afid:
            alive_fids.add(afid)
    logger.info(f"[folder_sync] 活檔索引 {len(key2files)} key / {len(md52files)} md5 / alive fileId {len(alive_fids)}")

    # 對每個 candidate 查 Drive 當前檔名
    updates = []  # batchUpdate body
    stats = Counter()
    start = time.time()
    inaccessible_fids = []  # list of (row_num, fid, title, status)
    backfilled = []  # 回補成功 (row_num, old_fid, new_fid, new_name)、用於通知與清 marker
    review_rows = []  # 🛡 分歧守門攔下、未覆寫的疑似錯位 (row_num, old_title, new_name, note_stem, fid)

    def _try_backfill_or_mark(row_num, fid, title, note_stem, reason, md5=None):
        """死連結：先試索引回補 re-link；找不到才列入 inaccessible（→標 DRIVE_GONE）。

        回補優先序：① md5（byte 相同最可靠，今日 quarantine 清隔離副本→孤兒的根治）
        ② 檔名（舊邏輯，套守門 overlap）。md5 索引缺（F 欄未寫）時自動只走檔名。
        """
        hit = find_backfill_md5(md52files, alive_fids, md5)
        how = "md5"
        if not hit:
            hit = find_backfill(key2files, alive_fids, title, note_stem)
            how = "name"
        if hit:
            new_fid, new_name = hit
            alive_fids.add(new_fid)
            updates.append({
                "range": f"'ALL'!U{row_num}",
                "values": [[f"https://drive.google.com/file/d/{new_fid}/view?usp=drivesdk"]],
            })
            backfilled.append((row_num, fid, new_fid, new_name))
            stats["backfilled"] += 1
            stats[f"backfilled_by_{how}"] += 1
            logger.info(f"[folder_sync] ♻ 回補 row {row_num}：死連結→活檔「{new_name[:40]}」(by {how}, {reason})")
            return
        inaccessible_fids.append((row_num, fid, title, reason))

    for i, (row_num, fid, title, note_stem) in enumerate(candidates, 1):
        meta, err_status = fetch_drive_meta_with_retry(drive_service, fid, retry_times, retry_delay)
        if meta is None:
            if err_status == 404:
                stats["dead_404"] += 1
                _try_backfill_or_mark(row_num, fid, title, note_stem, "404 not found")
            elif err_status == 403:
                stats["dead_403"] += 1
                _try_backfill_or_mark(row_num, fid, title, note_stem, "403 permission denied")
            else:
                stats[f"meta_err_{err_status}"] += 1
                if err_status:
                    _try_backfill_or_mark(row_num, fid, title, note_stem, f"err {err_status}")
            continue

        if meta.get("trashed"):
            stats["trashed"] += 1
            # trashed 檔仍可取 md5Checksum → 優先用 md5 回補（quarantine 清隔離副本→孤兒的根治）
            _try_backfill_or_mark(row_num, fid, title, note_stem, "trashed", md5=meta.get("md5Checksum"))
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

    # ♻ 自我修復：對既有 DRIVE_GONE 列做一次索引比對。
    #   先純記憶體檔名比對（不打 Drive API）；檔名沒中再用該列 fileId 取 md5 做 byte 比對
    #   （md5 fetch 只發生在「檔名沒中」的 DRIVE_GONE 列、數量有限，救回率高）。
    #   drive_index 刷新索引後，曾因索引 stale 或檔名對不上被標 DRIVE_GONE 但檔案其實在書庫的列
    #   會在此被自動救回 → 改寫 U + 清 T，不再永久卡死。
    self_healed = []  # (row_num, new_fid, new_name)
    for ridx, row in enumerate(rows, start=2):
        while len(row) <= COL_DRIVE_LINK:
            row.append("")
        if str(row[COL_MARKER]).strip() != "DRIVE_GONE":
            continue
        if str(row[COL_SOURCE]).strip() != SOURCE_FILTER:
            continue
        gtitle = str(row[COL_TITLE]).strip()
        gnote = extract_orig_stem(str(row[COL_NOTE]) if len(row) > COL_NOTE else "")
        hit = find_backfill(key2files, alive_fids, gtitle, gnote)
        if not hit and md52files:
            # 檔名沒中 → 用該列原 fileId 取 md5（trashed 仍可取）做 byte 比對
            gfid = extract_file_id(str(row[COL_DRIVE_LINK]))
            if gfid:
                try:
                    gm = drive_service.files().get(
                        fileId=gfid, fields="md5Checksum", supportsAllDrives=True).execute()
                    hit = find_backfill_md5(md52files, alive_fids, gm.get("md5Checksum"))
                    if hit:
                        stats["self_healed_by_md5"] += 1
                except HttpError:
                    pass
        if not hit:
            continue
        new_fid, new_name = hit
        alive_fids.add(new_fid)
        updates.append({
            "range": f"'ALL'!U{ridx}",
            "values": [[f"https://drive.google.com/file/d/{new_fid}/view?usp=drivesdk"]],
        })
        self_healed.append((ridx, new_fid, new_name))
        stats["self_healed"] += 1
    if self_healed:
        logger.info(f"[folder_sync] ♻ 自我修復既有 DRIVE_GONE {len(self_healed)} 列（索引刷新後救回）")

    # 把這次新發現不可訪問的列 T 欄打 DRIVE_GONE marker，下次 cron 自動跳過
    marker_updates = [
        {"range": f"'ALL'!T{row_num}", "values": [["DRIVE_GONE"]]}
        for row_num, _fid, _title, _reason in inaccessible_fids
    ]
    # 回補/自我修復成功的列：清掉 T（DRIVE_GONE）讓它重回正常流程
    clear_marker_updates = [
        {"range": f"'ALL'!T{row_num}", "values": [[""]]}
        for row_num, *_ in (backfilled + self_healed)
    ]
    all_updates = updates + marker_updates + clear_marker_updates

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

    # ♻ 回補通知：死連結但在書庫找到活檔、已自動 re-link（含本次新發現回補 + 既有 DRIVE_GONE 自我修復）
    total_backfill = len(backfilled) + len(self_healed)
    if total_backfill:
        blines = [f"♻ folder_sync：{total_backfill} 個死連結自動回補（檔案被重傳/搬動換了 fileId、仍在書庫）→ 已 re-link"]
        if backfilled:
            blines.append(f"  本次掃描新回補 {len(backfilled)}；既有 DRIVE_GONE 自我修復 {len(self_healed)}")
        for row_num, _ofid, _nfid, new_name in backfilled[:10]:
            blines.append(f"  row {row_num} | → 活檔「{new_name[:40]}」")
        for row_num, _nfid, new_name in self_healed[:10]:
            blines.append(f"  row {row_num} | (自我修復) → 活檔「{new_name[:40]}」")
        try:
            notify_success("\n".join(blines))
        except Exception as e:
            logger.warning(f"  backfill notify err: {e}")

    # 不可訪問檔案通知：只在「這次新發現、且回補也找不到」時發一次（DRIVE_GONE 已標的列上面已跳過）
    if inaccessible_fids:
        lines = [f"⚠️ folder_sync：本次新發現 {len(inaccessible_fids)} 個 Drive 檔案無法訪問（404/403/trashed）且書庫索引找不到回補活檔，已自動標 T=DRIVE_GONE，未來 cron 不再重打"]
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
        "backfilled_count": len(backfilled),
        "self_healed_count": len(self_healed),
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
