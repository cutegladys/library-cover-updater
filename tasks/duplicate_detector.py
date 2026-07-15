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

from utils.oauth import load_creds
from utils.sheets import sheet_id
from utils.notify import notify_error, notify_with_buttons
from tasks.duplicate_safety import assess_groups, fetch_drive_metadata

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


def _legacy_scan_duplicate_groups(all_data):
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


def collect_duplicate_groups(all_data):
    """Collect every repeated title once; author equality is not identity."""
    title_groups = defaultdict(
        lambda: {"title": "", "rows": [], "row_numbers": []}
    )
    for index, row in enumerate(all_data):
        row_number = index + 2
        title = str(row[0]).strip() if row else ""
        if not title:
            continue
        group = title_groups[title.lower()]
        if not group["title"]:
            group["title"] = title
        group["rows"].append(row)
        group["row_numbers"].append(row_number)

    groups = []
    for group in title_groups.values():
        if len(group["rows"]) < 2:
            continue
        authors = [
            str(row[1]).strip() if len(row) > 1 else ""
            for row in group["rows"]
        ]
        unique_authors = sorted({author for author in authors if author})
        groups.append(
            {
                **group,
                "type": "title",
                "author": unique_authors[0] if len(unique_authors) == 1 else "",
                "authors": authors,
                "unique_authors": unique_authors,
            }
        )
    return groups


def scan_duplicate_groups(all_data, file_meta=None):
    """Fail closed: without Drive evidence, no title-only group is AUTO."""
    return assess_groups(collect_duplicate_groups(all_data), file_meta or {})


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")

    creds = load_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[duplicate_detector] 讀 ALL 分頁...")
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range="'ALL'!A2:V",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    all_data = res.get("values", [])
    logger.info(f"[duplicate_detector] ALL 行數: {len(all_data)}")

    candidates = collect_duplicate_groups(all_data)
    drive_service = build("drive", "v3", credentials=creds)
    file_meta = fetch_drive_metadata(drive_service, candidates)
    groups = assess_groups(candidates, file_meta)
    auto_count = len(groups["auto_mergeable"])
    manual_count = len(groups["manual_review"])
    logger.info(f"[duplicate_detector] auto_mergeable: {auto_count}, manual_review: {manual_count}")

    # _DupCandidates is a derived view, including the empty result.
    ensure_sheet(sheets_service, sid, CANDIDATES_SHEET, dry_run)

    if auto_count == 0 and manual_count == 0:
        if not dry_run:
            sheets_service.spreadsheets().values().clear(
                spreadsheetId=sid, range=f"'{CANDIDATES_SHEET}'!A2:F",
            ).execute()
        return {"task": "duplicate_detector", "targets": 0, "note": "no_duplicates"}

    # 寫 _DupCandidates 分頁

    rows = [["Group", "Type", "Title", "Author(s)", "Row Numbers", "Action"]]
    for i, g in enumerate(groups["auto_mergeable"], 1):
        rows.append([
            f"AUTO-{i}",
            f"AUTO_SAFE：{g['safety_class']}",
            g["title"][:100],
            " | ".join(g.get("unique_authors", []))[:200],
            ", ".join(str(n) for n in g["row_numbers"]),
            "批准後 LIVE 合併；merger 會重驗 Drive 證據",
        ])
    for i, g in enumerate(groups["manual_review"], 1):
        rows.append([
            f"MANUAL-{i}",
            f"MANUAL_REVIEW：{g['safety_class']}",
            g["title"][:100],
            " | ".join(g.get("unique_authors", []))[:200],
            ", ".join(str(n) for n in g["row_numbers"]),
            "逐組驗 fileId／MD5／版本／媒體；不可全域強制合併",
        ])

    # ⚠️ Reconcile 邏輯（保留 user 標記 + 自動清理已合併 / 已處理 group）
    # 規則：
    #   既有 in 當前 set → 保留（user 可能還在 review）
    #   既有 NOT in 當前 set + 操作 ∈ (DONE, MERGED, REJECTED, IGNORED) → 刪（已處理）
    #   既有 NOT in 當前 set + 預設值「自動合併 / 人工 review」 → 刪（表示已被 merge、不再重複）
    #   新 group NOT in 既有 → append
    existing_data = []  # list of (row_num, key, action)
    try:
        existing_res = sheets_service.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{CANDIDATES_SHEET}'!A1:F",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        existing_vals = existing_res.get("values", [])
        if existing_vals:
            has_header_row = str(existing_vals[0][0] if len(existing_vals[0]) > 0 else "").strip() in ("Group",)
            start_idx = 1 if has_header_row else 0
            for i, r in enumerate(existing_vals[start_idx:], start=start_idx + 1):
                while len(r) < 6:
                    r.append("")
                title = str(r[2]).strip().lower()
                author = str(r[3]).strip().lower()
                action = str(r[5]).strip().upper()
                if title:
                    existing_data.append((i, (title, author), action))
    except HttpError as e:
        if e.resp.status != 400:
            raise

    existing_key_set = {key for _, key, _ in existing_data}
    current_keys = set()
    for g in groups["auto_mergeable"]:
        current_keys.add((g["title"].strip().lower(), g.get("author", "").strip().lower()))
    for g in groups["manual_review"]:
        current_keys.add((g["title"].strip().lower(), ""))  # title 同/author 不同類別、key 用 (title, "")

    logger.info(f"[duplicate_detector] _DupCandidates 既有 {len(existing_data)} 列、當前 group {len(current_keys)}")

    # 刪：既有 NOT in 當前 → 已被合併 / 已不重複（不管 action）
    rows_to_delete = [row_num for row_num, key, _action in existing_data if key not in current_keys]
    rows_to_delete.sort(reverse=True)

    # Append：當前 NOT in 既有
    new_rows = [r for r in rows[1:]
                if (str(r[2]).strip().lower(), str(r[3]).strip().lower() if str(r[1]).startswith("title+author") else "") not in existing_key_set
                and (str(r[2]).strip().lower(), "") not in existing_key_set]
    # Note: 上面 key 邏輯複雜（auto vs manual key 不同），用更直接 fallback：兩種 key 都檢查
    new_rows = []
    for r in rows[1:]:
        title_k = str(r[2]).strip().lower()
        author_k = str(r[3]).strip().lower()
        if r[1].startswith("title+author"):
            key = (title_k, author_k)
        else:
            key = (title_k, "")
        if key not in existing_key_set:
            new_rows.append(r)

    has_header_existing = bool(existing_data) or (existing_vals and len(existing_vals[0]) > 0)
    if not has_header_existing:
        new_rows = [rows[0]] + new_rows

    logger.info(f"[duplicate_detector] 計畫：append {len(new_rows)} 列、刪 {len(rows_to_delete)} 列已處理")

    if False and not dry_run:  # legacy reconcile disabled; full rebuild below is authoritative
        if rows_to_delete:
            meta = sheets_service.spreadsheets().get(spreadsheetId=sid).execute()
            dup_sheet_id = next((s["properties"]["sheetId"] for s in meta["sheets"]
                                 if s["properties"]["title"] == CANDIDATES_SHEET), None)
            if dup_sheet_id is not None:
                delete_requests = [
                    {"deleteDimension": {"range": {
                        "sheetId": dup_sheet_id, "dimension": "ROWS",
                        "startIndex": r - 1, "endIndex": r,
                    }}} for r in rows_to_delete
                ]
                sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=sid, body={"requests": delete_requests},
                ).execute()
        if new_rows:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=sid,
                range=f"'{CANDIDATES_SHEET}'!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": new_rows},
            ).execute()

        # ── Group 編號 reconcile（修撞號 bug）─────────────────────────────
        # 既有 row 保留舊編號 + 新 append row 從 1 重新 enumerate → 不同次 scan 寫入會撞號
        # （例 MANUAL-2 出現兩本不同書）。修法：append 完整體再讀一次 A/B 欄，
        # 按 row 順序對 AUTO / MANUAL 各自重編，只寫回 A 欄（保留 Action 欄 user 標記）。
        try:
            cur = sheets_service.spreadsheets().values().get(
                spreadsheetId=sid, range=f"'{CANDIDATES_SHEET}'!A1:B",
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute().get("values", [])
            if len(cur) > 1:
                auto_n = 0
                manual_n = 0
                new_col_a = [["Group"]]  # header
                for r in cur[1:]:
                    typ = str(r[1] if len(r) > 1 else "").strip()
                    if typ.startswith("title+author"):
                        auto_n += 1
                        new_col_a.append([f"AUTO-{auto_n}"])
                    elif typ.startswith("title"):
                        manual_n += 1
                        new_col_a.append([f"MANUAL-{manual_n}"])
                    else:
                        new_col_a.append([r[0] if len(r) > 0 else ""])
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=sid,
                    range=f"'{CANDIDATES_SHEET}'!A1:A{len(new_col_a)}",
                    valueInputOption="RAW",
                    body={"values": new_col_a},
                ).execute()
                logger.info(f"[duplicate_detector] Group 編號 reconcile：AUTO={auto_n}, MANUAL={manual_n}")
        except Exception as e:
            logger.warning(f"[duplicate_detector] Group 編號 reconcile 失敗（不影響合併）：{e}")

    # _DupCandidates is a derived view. Rebuild it from current evidence so an
    # old title+author verdict can never survive a safer detector run.
    if not dry_run:
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"'{CANDIDATES_SHEET}'!A:F",
        ).execute()
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{CANDIDATES_SHEET}'!A1:F{len(rows)}",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

    # 通知含 inline button（D2 Telegram bridge）
    summary = (
        f"📋 Library 重複偵測完成\n"
        f"  auto_mergeable: {auto_count} 組（同 fileId／同非空 MD5）\n"
        f"  manual_review: {manual_count} 組（同書名，但檔案身分未證明相同）\n\n"
        f"請先查看 _DupCandidates 分頁 review。\n"
        f"點 ✅ 批准後，Zeabur 5 分鐘內會 LIVE 合併 AUTO-*；刪列前會再次查 Drive 證據。\n"
        f"MANUAL-* 不提供全域強制合併。"
    )
    if auto_count > 0:
        notify_with_buttons(summary, [[
            {"text": "✅ 批准排隊合併", "callback_data": "lib_merge_now"},
            {"text": "❌ 略過", "callback_data": "lib_merge_skip"},
        ]])
    else:
        notify_error(summary)

    # manual_review 額外獨立通知；刻意不提供 global force merge。
    if manual_count > 0:
        sample_lines = [f"⚠️ Library 有 {manual_count} 組同書名候選需要人工 review："]
        for i, g in enumerate(groups["manual_review"][:10], 1):
            authors_str = " | ".join(g.get("unique_authors", []))[:80]
            sample_lines.append(
                f"  {i}. 《{g['title'][:40]}》 → {g['safety_class']} | {authors_str}"
            )
        if manual_count > 10:
            sample_lines.append(f"  ...另 {manual_count - 10} 組")
        sample_lines.append("")
        sample_lines.append("請逐組驗 fileId／MD5／版本／媒體；禁止全域強制合併。")
        manual_msg = "\n".join(sample_lines)
        try:
            notify_error(manual_msg)
        except Exception as e:
            logger.warning(f"  manual_review 通知失敗：{e}")

    return {
        "task": "duplicate_detector",
        "targets": auto_count + manual_count,
        "elapsed_sec": 0,
        "stats": {"auto_mergeable": auto_count, "manual_review": manual_count, "rows_written": len(rows)},
    }
