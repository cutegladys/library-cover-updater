"""
Library Health Dashboard — 純資料版（無 GAS cell formatting）

對應 healthDashboard.js buildHealthDashboard (451 行)，簡化成 ~150 行 Python。
GAS 原版含 merge/setBackground/setColumnWidth/setFrozenRows 等 formatting，
Python 版只寫資料、不做 formatting（user 可在 sheet UI 手動套樣式、或之後加 formatting）。

寫 Health_Dashboard 分頁四個區塊：
  區塊 1：系統摘要（Drive 索引、ALL 書目、覆蓋率）
  區塊 2：封面狀況（缺封面分布）
  區塊 3：Orphan 孤兒（Drive 有檔但 ALL 無書目）前 20 筆
  區塊 4：Fallback 失配（最近一次 linkDriveFilesFromNote 未配對）前 20 筆

env：
  COVER_UPDATER_DRY_RUN  1 = 只印不寫
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.health_dashboard")

DASH_SHEET = "Health_Dashboard"
INDEX_SHEET = "檔案清單_Export"
ALL_SHEET = "ALL"
FALLBACK_SHEET = "Fallback_Log"

MAX_ORPHAN = 20
MAX_FALLBACK = 20

TW_TZ = timezone(timedelta(hours=8))


def now_tw_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")


def get_sheet_id_by_name(sheets_service, spreadsheet_id, name):
    """找 sheet 的 sheetId（用於 spreadsheets.batchUpdate addSheet 前確認）。返 None 表沒有。"""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == name:
            return s["properties"]["sheetId"]
    return None


def ensure_dashboard_sheet(sheets_service, spreadsheet_id, dry_run):
    """確保 Health_Dashboard 分頁存在。返 sheetId。"""
    sid = get_sheet_id_by_name(sheets_service, spreadsheet_id, DASH_SHEET)
    if sid is not None:
        return sid
    if dry_run:
        logger.info(f"[health_dashboard] (dry_run) Would create sheet '{DASH_SHEET}'")
        return None
    res = sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{"addSheet": {"properties": {"title": DASH_SHEET}}}],
        },
    ).execute()
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]


def read_sheet_values(sheets_service, spreadsheet_id, range_a1, render="UNFORMATTED_VALUE"):
    """讀 sheet 範圍；如果分頁不存在會 raise HttpError，由 caller 處理。"""
    try:
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueRenderOption=render,
        ).execute()
        return res.get("values", [])
    except HttpError as e:
        if e.resp.status == 400:
            # range invalid（分頁不存在）
            return None
        raise


def parse_iso(s: str):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s[:16] if s else ""


def build_summary_rows(index_data, all_data):
    """區塊 1：系統摘要。返 list of [label, value]。"""
    rows = []

    if index_data is None:
        rows.append(["Drive 索引", "⚠️ 找不到「檔案清單_Export」分頁"])
    else:
        idx_count = max(0, len(index_data))
        last_updated = ""
        if index_data:
            row = index_data[0]
            # E 欄 = index 4，LastUpdated
            if len(row) > 4 and row[4]:
                last_updated = parse_iso(str(row[4]))
        rows.append(["Drive 索引總筆數", f"{idx_count} 筆"])
        rows.append(["索引最後更新", last_updated or "（未知）"])

    if all_data:
        all_count = len(all_data)
        drive_count = 0
        covered_v = 0
        covered_u = 0
        has_cover = 0
        for r in all_data:
            while len(r) < 22:
                r.append("")
            src = str(r[5]).strip()  # F
            file_id = str(r[21]).strip()  # V
            url = str(r[20]).strip()  # U
            cover = str(r[16]).strip()  # Q
            if src == "Google Drive":
                drive_count += 1
                if file_id:
                    covered_v += 1
                if url:
                    covered_u += 1
            if cover:
                has_cover += 1

        cov_v = round(covered_v / drive_count * 1000) / 10 if drive_count else 0
        cov_u = round(covered_u / drive_count * 1000) / 10 if drive_count else 0
        cov_q = round(has_cover / all_count * 1000) / 10 if all_count else 0

        rows.append(["ALL 書目總列數", f"{all_count} 列"])
        rows.append(["Google Drive 來源", f"{drive_count} 筆"])
        rows.append(["FileId 覆蓋率（V 欄）", f"{cov_v}% ({covered_v}/{drive_count})"])
        rows.append(["Drive URL 覆蓋率（U 欄）", f"{cov_u}% ({covered_u}/{drive_count})"])
        rows.append(["封面覆蓋率（Q 欄）", f"{cov_q}% ({has_cover}/{all_count})"])

    return rows


def build_cover_stats_rows(all_data):
    """區塊 2：封面狀況。"""
    rows = []
    if not all_data:
        rows.append(["（無資料）", ""])
        return rows

    nocover_drive = 0
    nocover_myon = 0
    nocover_other = 0
    for r in all_data:
        while len(r) < 22:
            r.append("")
        src = str(r[5]).strip()
        cover = str(r[16]).strip()
        if cover:
            continue
        if src == "Google Drive":
            nocover_drive += 1
        elif src == "myOn":
            nocover_myon += 1
        else:
            nocover_other += 1

    rows.append(["Google Drive 來源缺封面", f"{nocover_drive} 筆（cover_drive 週日自動補）"])
    rows.append(["myOn 來源缺封面", f"{nocover_myon} 筆（cover_web 週一網路搜尋）"])
    rows.append(["其他來源缺封面", f"{nocover_other} 筆"])
    return rows


def build_orphan_rows(index_data, all_data):
    """區塊 3：Orphan（Drive 有檔、ALL 無書目）前 N 筆。"""
    rows = [["孤兒總數", "（找不到索引分頁）"]]
    if index_data is None:
        return rows

    known_fids = set()
    if all_data:
        for r in all_data:
            while len(r) < 22:
                r.append("")
            fid = str(r[21]).strip()  # V 欄
            if fid:
                known_fids.add(fid)

    orphans = []
    for i, r in enumerate(index_data, start=2):
        while len(r) < 4:
            r.append("")
        file_name = str(r[0]).strip()
        file_id = str(r[1]).strip()
        folder_path = str(r[3]).strip()
        if not file_id or file_id in known_fids:
            continue
        orphans.append({
            "row": i,
            "name": file_name,
            "fid": file_id,
            "path": folder_path,
        })

    rows = [["孤兒總數", f"{len(orphans)} 筆" + (f"（顯示前 {MAX_ORPHAN}）" if len(orphans) > MAX_ORPHAN else "")]]

    if not orphans:
        rows.append(["✅ 無孤兒", "所有 Drive 檔案均已有對應書目"])
        return rows

    # table header
    rows.append(["— 檔名 —", "— FileId —"])
    for o in orphans[:MAX_ORPHAN]:
        rows.append([o["name"][:80], o["fid"]])
    if len(orphans) > MAX_ORPHAN:
        rows.append(["...", f"尚有 {len(orphans) - MAX_ORPHAN} 筆"])
    return rows


def build_fallback_rows(fb_data):
    """區塊 4：Fallback 失配。"""
    if fb_data is None:
        return [["Fallback_Log", "（找不到分頁、無資料）"]]
    if not fb_data:
        return [["✅ Fallback_Log 為空", "無失配記錄"]]
    rows = [["失配總筆數", f"{len(fb_data)} 筆" + (f"（顯示前 {MAX_FALLBACK}）" if len(fb_data) > MAX_FALLBACK else "")]]
    rows.append(["— 行 —", "— 內容 —"])
    for i, r in enumerate(fb_data[:MAX_FALLBACK], start=2):
        rows.append([f"行 {i}", " | ".join(str(x)[:50] for x in r[:5])])
    return rows


def assemble_dashboard(index_data, all_data, fb_data):
    """組裝整個 dashboard 內容、返 2D list (row, col)。"""
    all_rows = []

    # 區塊 0：標題
    all_rows.append([f"📚 Library Health Dashboard　更新時間：{now_tw_str()}"])
    all_rows.append([f"每日 04:00 UTC 自動更新（Zeabur library-cover-updater）"])
    all_rows.append([""])

    # 區塊 1：系統摘要
    all_rows.append(["📊 系統摘要"])
    all_rows.extend(build_summary_rows(index_data, all_data))
    all_rows.append([""])

    # 區塊 2：封面狀況
    all_rows.append(["🖼️ 封面狀況"])
    all_rows.extend(build_cover_stats_rows(all_data))
    all_rows.append([""])

    # 區塊 3：Orphan
    all_rows.append(["👻 Orphan（Drive 有檔、ALL 無書目）"])
    all_rows.extend(build_orphan_rows(index_data, all_data))
    all_rows.append([""])

    # 區塊 4：Fallback
    all_rows.append(["⚠️ Fallback 失配"])
    all_rows.extend(build_fallback_rows(fb_data))

    return all_rows


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[health_dashboard] Reading source sheets...")
    all_data = read_sheet_values(sheets_service, sid, f"'{ALL_SHEET}'!A2:V") or []
    index_data = read_sheet_values(sheets_service, sid, f"'{INDEX_SHEET}'!A2:E")
    fb_data = read_sheet_values(sheets_service, sid, f"'{FALLBACK_SHEET}'!A2:E")

    logger.info(f"[health_dashboard] ALL: {len(all_data)} rows, index: {len(index_data) if index_data is not None else 'N/A'}, fallback: {len(fb_data) if fb_data is not None else 'N/A'}")

    # 確保 Health_Dashboard 分頁存在
    ensure_dashboard_sheet(sheets_service, sid, dry_run)

    # 組裝內容
    content = assemble_dashboard(index_data, all_data, fb_data)
    logger.info(f"[health_dashboard] Assembled {len(content)} rows")

    if dry_run:
        logger.info("[health_dashboard] (dry_run) Would write the following:")
        for r in content[:10]:
            logger.info(f"  {r}")
        return {"task": "health_dashboard", "targets": len(content), "stats": {"dry_run_rows": len(content)}}

    # 清掉舊內容
    try:
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sid,
            range=f"'{DASH_SHEET}'!A:E",
        ).execute()
    except HttpError as e:
        logger.warning(f"[health_dashboard] clear failed: {e}")

    # 寫新內容
    try:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{DASH_SHEET}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": content},
        ).execute()
    except HttpError as e:
        logger.error(f"[health_dashboard] write err: {e}")
        return {"task": "health_dashboard", "targets": 0, "stats": {"write_err": str(e)}}

    logger.info(f"[health_dashboard] 完成，寫入 {len(content)} 列")
    return {
        "task": "health_dashboard",
        "targets": len(content),
        "elapsed_sec": 0,
        "stats": {"rows_written": len(content)},
    }
