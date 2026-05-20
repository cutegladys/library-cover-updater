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
from utils.notify import notify
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.health_dashboard")

DASH_SHEET = "Health_Dashboard"
INDEX_SHEET = "檔案清單_Export"
ALL_SHEET = "ALL"
FALLBACK_SHEET = "Fallback_Log"

MAX_ORPHAN = 20
MAX_FALLBACK = 20

TW_TZ = timezone(timedelta(hours=8))

# 顏色 (RGB 0-1 float 給 Sheets API)
# 對應 healthDashboard.js _HD.COLOR
COLOR_HEADER_BG = {"red": 0.102, "green": 0.451, "blue": 0.910}   # #1a73e8 Google 藍
COLOR_HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}          # #ffffff 白
COLOR_SECTION_BG = {"red": 0.910, "green": 0.941, "blue": 0.996}   # #e8f0fe 淡藍
COLOR_SECTION_FG = {"red": 0.102, "green": 0.451, "blue": 0.910}   # #1a73e8 藍
COLOR_LABEL_FG = {"red": 0.373, "green": 0.388, "blue": 0.408}     # #5f6368 灰

# 三色狀態（對應 GAS _HD.COLOR.OK_BG / WARN_BG / ALERT_BG）
COLOR_OK_BG = {"red": 0.910, "green": 0.965, "blue": 0.918}        # #e8f5e9 淡綠
COLOR_WARN_BG = {"red": 1.0, "green": 0.953, "blue": 0.776}        # #fff3c4 淡黃
COLOR_ALERT_BG = {"red": 0.984, "green": 0.871, "blue": 0.871}     # #fbdede 淡紅


def status_emoji(s: str) -> str:
    return {"ok": "🟢", "warn": "🟡", "alert": "🔴"}.get(s, "")


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
    """區塊 1：系統摘要。返 list of [label, value, status]。"""
    rows = []

    if index_data is None:
        rows.append(["Drive 索引", "⚠️ 找不到「檔案清單_Export」分頁、請執行 drive_index task", "alert"])
    else:
        idx_count = max(0, len(index_data))
        last_updated = ""
        if index_data:
            row = index_data[0]
            if len(row) > 4 and row[4]:
                last_updated = parse_iso(str(row[4]))
        rows.append(["Drive 索引總筆數", f"{idx_count} 筆", "ok" if idx_count > 0 else "alert"])
        rows.append(["索引最後更新", last_updated or "（未知）", "neutral"])

    if all_data:
        all_count = len(all_data)
        drive_count = 0
        covered_v = 0
        covered_u = 0
        has_cover = 0
        for r in all_data:
            while len(r) < 22:
                r.append("")
            src = str(r[5]).strip()
            file_id = str(r[21]).strip()
            url = str(r[20]).strip()
            cover = str(r[16]).strip()
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

        rows.append(["ALL 書目總列數", f"{all_count} 列", "neutral"])
        rows.append(["Google Drive 來源", f"{drive_count} 筆", "neutral"])
        rows.append([
            "FileId 覆蓋率（V 欄）", f"{cov_v}% ({covered_v}/{drive_count})",
            "ok" if cov_v >= 98 else "warn" if cov_v >= 85 else "alert",
        ])
        rows.append([
            "Drive URL 覆蓋率（U 欄）", f"{cov_u}% ({covered_u}/{drive_count})",
            "ok" if cov_u >= 95 else "warn",
        ])
        rows.append([
            "封面覆蓋率（Q 欄）", f"{cov_q}% ({has_cover}/{all_count})",
            "ok" if cov_q >= 70 else "warn",
        ])

    return rows


def build_cover_stats_rows(all_data):
    """區塊 2：封面狀況。返 list of [label, value, status]。"""
    rows = []
    if not all_data:
        rows.append(["（無資料）", "", "neutral"])
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

    rows.append([
        "Google Drive 來源缺封面",
        f"{nocover_drive} 筆（cover_drive 週日自動補）",
        "ok" if nocover_drive == 0 else "warn" if nocover_drive < 50 else "alert",
    ])
    rows.append([
        "myOn 來源缺封面",
        f"{nocover_myon} 筆（cover_web 週一網路搜尋）",
        "ok" if nocover_myon == 0 else "warn",
    ])
    rows.append([
        "其他來源缺封面",
        f"{nocover_other} 筆",
        "ok" if nocover_other == 0 else "neutral",
    ])
    return rows


def build_orphan_rows(index_data, all_data):
    """區塊 3：Orphan（Drive 有檔、ALL 無書目）前 N 筆。返 (rows, orphan_count)。"""
    if index_data is None:
        return [["孤兒總數", "（找不到索引分頁）", "alert"]], 0

    known_fids = set()
    if all_data:
        for r in all_data:
            while len(r) < 22:
                r.append("")
            fid = str(r[21]).strip()
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
        orphans.append({"row": i, "name": file_name, "fid": file_id, "path": folder_path})

    orphan_status = "ok" if len(orphans) == 0 else "warn" if len(orphans) < 50 else "alert"
    rows = [["孤兒總數",
             f"{len(orphans)} 筆" + (f"（顯示前 {MAX_ORPHAN}）" if len(orphans) > MAX_ORPHAN else ""),
             orphan_status]]

    if not orphans:
        rows.append(["✅ 無孤兒", "所有 Drive 檔案均已有對應書目", "ok"])
        return rows, 0

    rows.append(["— 檔名 —", "— FileId —", "neutral"])
    for o in orphans[:MAX_ORPHAN]:
        rows.append([o["name"][:80], o["fid"], "neutral"])
    if len(orphans) > MAX_ORPHAN:
        rows.append(["...", f"尚有 {len(orphans) - MAX_ORPHAN} 筆", "neutral"])
    return rows, len(orphans)


def build_fallback_rows(fb_data):
    """區塊 4：Fallback 失配。Fallback_Log schema 預設 [timestamp, type, message, details...]。"""
    if fb_data is None:
        return [["Fallback_Log", "（找不到分頁、無資料）", "neutral"]], 0
    if not fb_data:
        return [["✅ Fallback_Log 為空", "無失配記錄", "ok"]], 0

    fb_status = "ok" if len(fb_data) < 10 else "warn" if len(fb_data) < 50 else "alert"
    rows = [["失配總筆數",
             f"{len(fb_data)} 筆" + (f"（顯示前 {MAX_FALLBACK}）" if len(fb_data) > MAX_FALLBACK else ""),
             fb_status]]
    rows.append(["— 時間 —", "— 訊息 —", "neutral"])
    for r in fb_data[:MAX_FALLBACK]:
        # 嘗試 parse：第 1 欄假設為 timestamp、其餘合併為訊息
        while len(r) < 3:
            r.append("")
        ts = parse_iso(str(r[0])) if r[0] else "—"
        msg = " | ".join(str(x)[:60] for x in r[1:4] if x)
        rows.append([ts, msg or "(empty)", "neutral"])
    return rows, len(fb_data)


def assemble_dashboard(index_data, all_data, fb_data):
    """組裝整個 dashboard 內容。返 (sheet_rows[label,value], statuses[row_idx -> 'ok'/'warn'/'alert'/'neutral'], summary)。

    summary：拿來判斷要不要發 Telegram。
    """
    all_rows = []   # 寫 sheet（2 欄）
    statuses = {}   # row_idx (0-based) → status string

    def add(label, value="", status="neutral"):
        all_rows.append([label, value])
        statuses[len(all_rows) - 1] = status

    def add_block(block_rows):
        for r in block_rows:
            label = r[0] if len(r) > 0 else ""
            value = r[1] if len(r) > 1 else ""
            status = r[2] if len(r) > 2 else "neutral"
            add(label, value, status)

    add(f"📚 Library Health Dashboard　更新時間：{now_tw_str()}", "", "neutral")
    add("每日 04:00 UTC 自動更新（Zeabur library-cover-updater）", "", "neutral")
    add("")

    add("📊 系統摘要", "", "section")
    add_block(build_summary_rows(index_data, all_data))
    add("")

    add("🖼️ 封面狀況", "", "section")
    add_block(build_cover_stats_rows(all_data))
    add("")

    add("👻 Orphan（Drive 有檔、ALL 無書目）", "", "section")
    orphan_rows, orphan_count = build_orphan_rows(index_data, all_data)
    add_block(orphan_rows)
    add("")

    add("⚠️ Fallback 失配", "", "section")
    fb_rows, fb_count = build_fallback_rows(fb_data)
    add_block(fb_rows)

    # 統計 overall worst status
    worst = "ok"
    rank = {"ok": 0, "neutral": 0, "warn": 1, "alert": 2}
    for s in statuses.values():
        if rank.get(s, 0) > rank.get(worst, 0):
            worst = s

    return all_rows, statuses, {
        "overall": worst,
        "orphan_count": orphan_count,
        "fallback_count": fb_count,
    }


def apply_formatting(sheets_service, spreadsheet_id, sheet_id, content, statuses=None):
    """套用 cell formatting：標題列、區塊標題、ok/warn/alert 三色、凍結 + column width。"""
    if statuses is None:
        statuses = {}
    requests = []

    # 1. 凍結首 2 列 + 標題列 column width
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 2},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # column A 寬 200, column B 寬 400（pixel）
    for col, width in [(0, 200), (1, 400)]:
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": col,
                    "endIndex": col + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # 2. 第 1-2 列（標題 + 副標題）藍底白字 bold center
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 2,
                "startColumnIndex": 0,
                "endColumnIndex": 5,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_HEADER_BG,
                    "textFormat": {
                        "foregroundColor": COLOR_HEADER_FG,
                        "bold": True,
                        "fontSize": 12,
                    },
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    })

    # 3. 區塊標題 + label/value + ok/warn/alert 三色
    section_emojis = ("📊 ", "🖼️ ", "👻 ", "⚠️ ")
    for i, row in enumerate(content):
        if not row:
            continue
        first = str(row[0])
        status = statuses.get(i, "neutral")
        # ok/warn/alert 三色背景套 value 欄（B 欄）
        if status in ("ok", "warn", "alert"):
            bg = {"ok": COLOR_OK_BG, "warn": COLOR_WARN_BG, "alert": COLOR_ALERT_BG}[status]
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": 1,
                        "endColumnIndex": 5,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
        if first.startswith(section_emojis):
            # section header 套淺藍底藍字 bold
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 5,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLOR_SECTION_BG,
                            "textFormat": {
                                "foregroundColor": COLOR_SECTION_FG,
                                "bold": True,
                                "fontSize": 11,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })
        elif len(row) >= 2 and row[1]:
            # label/value row：A 欄 label 灰 bold
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {
                                "foregroundColor": COLOR_LABEL_FG,
                                "bold": True,
                            }
                        }
                    },
                    "fields": "userEnteredFormat.textFormat",
                }
            })

    # batchUpdate 一次性送
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


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
    content, statuses, summary = assemble_dashboard(index_data, all_data, fb_data)
    logger.info(f"[health_dashboard] Assembled {len(content)} rows, overall={summary['overall']}")

    if dry_run:
        logger.info("[health_dashboard] (dry_run) Would write the following:")
        for r in content[:10]:
            logger.info(f"  {r}")
        return {"task": "health_dashboard", "targets": len(content), "stats": {"dry_run_rows": len(content), **summary}}

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

    # 套 formatting（背景色 / bold / 凍結 / column width）
    dash_sheet_id = get_sheet_id_by_name(sheets_service, sid, DASH_SHEET)
    if dash_sheet_id is not None:
        try:
            apply_formatting(sheets_service, sid, dash_sheet_id, content, statuses)
        except HttpError as e:
            logger.warning(f"[health_dashboard] formatting err（忽略，資料已寫）: {e}")

    # Telegram 通知（只在 warn / alert 時推；ok 不推、避免每日噪音）
    if summary["overall"] in ("warn", "alert"):
        emoji = status_emoji(summary["overall"])
        try:
            notify(
                f"{emoji} Library Health Dashboard 警示（{summary['overall'].upper()}）\n"
                f"  👻 Orphan: {summary['orphan_count']} 筆\n"
                f"  ⚠️ Fallback 失配: {summary['fallback_count']} 筆\n\n"
                f"請至試算表 Health_Dashboard 分頁查看詳情。",
                force=True,
            )
        except Exception as e:
            logger.warning(f"  Telegram notify err: {e}")

    logger.info(f"[health_dashboard] 完成，寫入 {len(content)} 列、overall={summary['overall']}")
    return {
        "task": "health_dashboard",
        "targets": len(content),
        "elapsed_sec": 0,
        "stats": {"rows_written": len(content), **summary},
    }
