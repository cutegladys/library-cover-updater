"""
Library 重複書偵測 — 寫 candidate list 到 _DupCandidates 分頁

對應 scanForDuplicateBooks.js 的 _scanDuplicateGroups (110 行)。

分兩類：
- auto_mergeable：同 title + 同 author（含 author 全空）→ 可自動合併
- manual_review：同 title + 不同 author → 要人工 review

只 detect、不執行 merge（merger 在 duplicate_merger.py、要 LIVE_MERGE=true 才跑）。

寫 _DupCandidates 分頁，欄位：
  Group | Type | Title | Author(s) | Row Numbers | Action

env:
  COVER_UPDATER_DRY_RUN  1 = 只印不寫
"""
import logging
import os
from collections import Counter, defaultdict

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_user_creds
from utils.sheets import sheet_id
from utils.notify import notify_error, notify_with_buttons

logger = logging.getLogger("library_cover_updater.duplicate_detector")

CANDIDATES_SHEET = "_DupCandidates"


def ensure_sheet(sheets_service, spreadsheet_id, name, dry_run):
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == name:
            return
    if dry_run:
        return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": name}}}]},
    ).execute()


def scan_duplicate_groups(all_data):
    """
    對應 _scanDuplicateGroups。
    all_data: list of row data，index 0 起，row_number 從 2 開始（跳標題）。
    回 dict {auto_mergeable: [...], manual_review: [...]}
    """
    seen_by_title = {}  # title_lower -> row_number
    title_groups = defaultdict(lambda: {"title": "", "rows": [], "row_numbers": []})
    seen_by_ta = {}  # "title|author" -> row_number
    ta_groups = defaultdict(lambda: {"title": "", "author": "", "rows": [], "row_numbers": []})

    for i, row in enumerate(all_data):
        row_num = i + 2
        title_raw = str(row[0]).strip() if len(row) > 0 else ""
        title_lower = title_raw.lower()
        author_raw = str(row[1]).strip() if len(row) > 1 else ""
        author_lower = author_raw.lower()

        if not title_lower:
            continue

        if title_lower in seen_by_title:
            grp = title_groups[title_lower]
            if not grp["title"]:
                # 第一次加入 dup group：把 original row 也加進
                orig_row_num = seen_by_title[title_lower]
                grp["title"] = title_raw
                grp["rows"].append(all_data[orig_row_num - 2])
                grp["row_numbers"].append(orig_row_num)
            grp["rows"].append(row)
            grp["row_numbers"].append(row_num)
        else:
            seen_by_title[title_lower] = row_num

        if author_lower:
            key = f"{title_lower}|{author_lower}"
            if key in seen_by_ta:
                grp = ta_groups[key]
                if not grp["title"]:
                    orig_row_num = seen_by_ta[key]
                    grp["title"] = title_raw
                    grp["author"] = author_raw
                    grp["rows"].append(all_data[orig_row_num - 2])
                    grp["row_numbers"].append(orig_row_num)
                grp["rows"].append(row)
                grp["row_numbers"].append(row_num)
            else:
                seen_by_ta[key] = row_num

    all_groups = []

    # title+author 同 → auto_mergeable
    for key, dup in ta_groups.items():
        if len(dup["rows"]) >= 2:
            all_groups.append({
                "type": "title_author",
                "title": dup["title"],
                "author": dup["author"],
                "rows": dup["rows"],
                "row_numbers": dup["row_numbers"],
                "auto_merge": True,
            })

    # title 同但 author 不同（或全空）
    for key, dup in title_groups.items():
        if len(dup["rows"]) < 2:
            continue
        # 看是否已被 title+author 處理
        is_handled = any(
            g["type"] == "title_author" and g["title"].lower() == dup["title"].lower()
            for g in all_groups
        )
        if is_handled:
            continue

        authors = [str(r[1]).strip() if len(r) > 1 else "" for r in dup["rows"]]
        non_empty = [a for a in authors if a]
        if not non_empty:
            # 全空 → auto_mergeable
            all_groups.append({
                "type": "title_author",
                "title": dup["title"],
                "author": "",
                "rows": dup["rows"],
                "row_numbers": dup["row_numbers"],
                "authors": authors,
                "unique_authors": [],
                "auto_merge": True,
            })
        else:
            # 不同 author → manual_review
            all_groups.append({
                "type": "title",
                "title": dup["title"],
                "rows": dup["rows"],
                "row_numbers": dup["row_numbers"],
                "authors": authors,
                "unique_authors": list(set(non_empty)),
                "auto_merge": False,
            })

    return {
        "auto_mergeable": [g for g in all_groups if g["auto_merge"]],
        "manual_review": [g for g in all_groups if not g["auto_merge"]],
    }


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    creds = load_user_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[duplicate_detector] 讀 ALL 分頁...")
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range="'ALL'!A2:I",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    all_data = res.get("values", [])
    logger.info(f"[duplicate_detector] ALL 行數: {len(all_data)}")

    groups = scan_duplicate_groups(all_data)
    auto_count = len(groups["auto_mergeable"])
    manual_count = len(groups["manual_review"])
    logger.info(f"[duplicate_detector] auto_mergeable: {auto_count}, manual_review: {manual_count}")

    if auto_count == 0 and manual_count == 0:
        return {"task": "duplicate_detector", "targets": 0, "note": "no_duplicates"}

    # 寫 _DupCandidates 分頁
    ensure_sheet(sheets_service, sid, CANDIDATES_SHEET, dry_run)

    rows = [["Group", "Type", "Title", "Author(s)", "Row Numbers", "Action"]]
    for i, g in enumerate(groups["auto_mergeable"], 1):
        rows.append([
            f"AUTO-{i}",
            "title+author 同",
            g["title"][:100],
            g.get("author", "")[:80],
            ", ".join(str(n) for n in g["row_numbers"]),
            "set LIVE_MERGE=true 自動合併",
        ])
    for i, g in enumerate(groups["manual_review"], 1):
        rows.append([
            f"MANUAL-{i}",
            "title 同/author 不同",
            g["title"][:100],
            " | ".join(g.get("unique_authors", []))[:200],
            ", ".join(str(n) for n in g["row_numbers"]),
            "人工 review、決定 master row",
        ])

    if not dry_run:
        # clear + write
        try:
            sheets_service.spreadsheets().values().clear(
                spreadsheetId=sid, range=f"'{CANDIDATES_SHEET}'!A:F",
            ).execute()
        except HttpError:
            pass
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{CANDIDATES_SHEET}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

    # 通知含 inline button（D2 Telegram bridge）
    summary = (
        f"📋 Library 重複偵測完成\n"
        f"  auto_mergeable: {auto_count} 組（同 title+author）\n"
        f"  manual_review: {manual_count} 組（title 同/author 不同）\n\n"
        f"請先查看 _DupCandidates 分頁 review。\n"
        f"點 ✅ 批准 → Zeabur 5 分鐘內自動合併 AUTO-* 組（merger 跑時也是 DRY_RUN 預設、實際合併要再 redeploy 設 LIVE_MERGE=true）。\n"
        f"或請 user 直接到 Zeabur dashboard 設 LIVE_MERGE=true Redeploy 強制 live。"
    )
    if auto_count > 0:
        notify_with_buttons(summary, [[
            {"text": "✅ 批准排隊合併", "callback_data": "lib_merge_now"},
            {"text": "❌ 略過", "callback_data": "lib_merge_skip"},
        ]])
    else:
        notify_error(summary)

    return {
        "task": "duplicate_detector",
        "targets": auto_count + manual_count,
        "elapsed_sec": 0,
        "stats": {"auto_mergeable": auto_count, "manual_review": manual_count, "rows_written": len(rows)},
    }
