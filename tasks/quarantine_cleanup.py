"""
Quarantine Safe Cleanup — 隔離區「零風險」自動清理

只清「md5 + size + 副檔名 三重與某個『非隔離存活檔』完全相同」的隔離檔。
位元組一模一樣 = 同一份東西在書庫別處還有 → 丟垃圾桶零損失。其餘一律保留。
丟「垃圾桶」(trashed=true，Drive 30 天可救回)，非永久刪除。

對應本機 Library/scripts/quarantine_safe_cleanup.py（同一套規則）。
背景：2026-06 大量書目誤刪事件後，隔離區無自動清理機制純堆積；此 task 補上
安全的定期清理（只碰 byte 完全相同的真重複，且預設只清進隔離 >30 天的）。

掃兩個書庫 root（同 drive_index）：
  1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx（英文讀本資源）
  1N-CIlms9t6HHyui52682bAC-gzVmZH3H（eBookReading）

判定「隔離」：路徑上任一資料夾名含 CLEAN_TOKENS 之一
  (_duplicates_quarantine / _archive_originals / _from_ / _conflict / _manual_review)
  ＊不含 _inbox / _extracted（活躍流水線，別碰）

env:
  QUARANTINE_CLEANUP_APPLY        true=真的丟垃圾桶；其餘=只報告(dry-run，預設)
  QUARANTINE_CLEANUP_MIN_AGE_DAYS 只清 modifiedTime 早於 N 天的（預設 30）
  COVER_UPDATER_DRY_RUN           全域 dry-run；1/true 時強制只報告
  DRIVE_INDEX_ROOT_IDS            覆寫 root id list（comma-separated）
"""
import logging
import os
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.relay import relay_call

logger = logging.getLogger("library_cover_updater.quarantine_cleanup")

DEFAULT_ROOT_IDS = [
    "1Ymchv8TDiEeMYgsbSPDXALXe-PFCcOvx",  # 英文讀本資源
    "1N-CIlms9t6HHyui52682bAC-gzVmZH3H",  # eBookReading
]
CLEAN_TOKENS = ("_duplicates_quarantine", "_archive_originals", "_from_", "_conflict", "_manual_review")
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # 0-byte 檔的 md5（會假比對，排除）
MIN_SIZE = 4096  # 小於此不納入（避免空檔/placeholder 共用 md5 假比對）


def _is_clean_folder(name: str) -> bool:
    n = (name or "").lower()
    return any(t in n for t in CLEAN_TOKENS)


def _ext(name: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", (name or "").strip())
    return m.group(1).lower() if m else ""


def run():
    roots = os.environ.get("DRIVE_INDEX_ROOT_IDS", "").strip()
    root_ids = [r.strip() for r in roots.split(",") if r.strip()] or DEFAULT_ROOT_IDS
    global_dry = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    apply = (os.environ.get("QUARANTINE_CLEANUP_APPLY", "false").lower() in ("1", "true", "yes")) and not global_dry
    min_age_days = int(os.environ.get("QUARANTINE_CLEANUP_MIN_AGE_DAYS", "30"))

    creds = load_creds()
    drive = build("drive", "v3", credentials=creds)

    # ── 遞迴 bulk 掃，帶 md5；in_quar 標記是否在隔離路徑下 ──
    quar = []        # dict(name,fid,md5,size,mtime,path)
    living = {}      # (md5,size,ext) -> (name,fid)
    nfiles = 0
    queue = [(r, "", False) for r in root_ids]
    while queue:
        folder_id, path, in_quar = queue.pop()
        # 檔案
        tok = None
        while True:
            try:
                resp = drive.files().list(
                    q=f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false",
                    fields="nextPageToken, files(id,name,md5Checksum,modifiedTime,size)",
                    pageSize=1000, pageToken=tok,
                    includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
            except HttpError as e:
                logger.warning(f"list files 失敗 {folder_id}: {e}")
                break
            for f in resp.get("files", []):
                nfiles += 1
                md5 = f.get("md5Checksum")
                size = int(f.get("size", 0) or 0)
                if in_quar:
                    quar.append({"name": f["name"], "fid": f["id"], "md5": md5,
                                 "size": size, "mtime": f.get("modifiedTime"), "path": path})
                else:
                    if md5 and md5 != EMPTY_MD5 and size >= MIN_SIZE:
                        living.setdefault((md5, size, _ext(f["name"])), (f["name"], f["id"]))
            tok = resp.get("nextPageToken")
            if not tok:
                break
        # 子資料夾
        tok = None
        while True:
            try:
                resp = drive.files().list(
                    q=f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                    fields="nextPageToken, files(id,name)", pageSize=1000, pageToken=tok,
                    includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
            except HttpError as e:
                logger.warning(f"list folders 失敗 {folder_id}: {e}")
                break
            for sub in resp.get("files", []):
                sub_q = in_quar or _is_clean_folder(sub["name"])
                queue.append((sub["id"], (path + "/" + sub["name"]) if path else sub["name"], sub_q))
            tok = resp.get("nextPageToken")
            if not tok:
                break

    # ── 評估：md5+size+ext 三重相同、size>=MIN_SIZE、非空md5、且(可選)年齡達標 ──
    now = datetime.now(timezone.utc)
    deletable = []
    tier2 = 0  # 有 md5 的真書、但無 byte 完全相同母本 → 需人工/AI 判斷(同書不同格式/版本 or 唯一份)
    for f in quar:
        md5, size = f["md5"], f["size"]
        if not md5 or md5 == EMPTY_MD5 or size < MIN_SIZE:
            continue
        twin = living.get((md5, size, _ext(f["name"])))
        if not twin:
            tier2 += 1
            continue
        if min_age_days > 0 and f["mtime"]:
            try:
                mt = datetime.fromisoformat(f["mtime"].replace("Z", "+00:00"))
                if (now - mt).days < min_age_days:
                    continue
            except Exception:
                pass
        deletable.append(f)

    total_mb = round(sum(d["size"] for d in deletable) / 1024 / 1024)
    trashed = failed = 0
    if apply:
        for f in deletable:
            try:
                relay_call("drive.trash", {"fileId": f["fid"]})
                trashed += 1
            except Exception as e:
                failed += 1
                logger.warning(f"trash 失敗 {f['name']}: {e}")

    stats = {
        "掃描檔數": nfiles,
        "隔離檔": len(quar),
        "確定可丟(byte相同)": len(deletable),
        "可丟容量MB": total_mb,
        "Tier2待人工審視": tier2,
    }
    if apply:
        stats["已丟垃圾桶"] = trashed
        if failed:
            stats["丟失敗"] = failed
    else:
        stats["模式"] = 0  # 0 = dry-run（未動任何檔）
    logger.info(f"quarantine_cleanup: apply={apply} min_age={min_age_days} "
                f"deletable={len(deletable)} trashed={trashed}")
    return stats
