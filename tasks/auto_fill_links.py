"""
Library 自動填入圖書館搜尋連結公式（J-P 欄）

對應 autoFillLibraryLinks.js。
只填「A 欄有書名 + J-O 欄位空」的列；P 欄 ARRAYFORMULA 只在 Row 2 設一次。

J-O 各欄獨立公式（per-row HYPERLINK 公式）：
  J: 全台電子書搜尋 (taiwanlibrarysearch)
  K: 新北 UDN
  L: 北市 UDN
  M: Pubu
  N: Lexile (Google search)
  O: Scholastic 年級

P 欄：ARRAYFORMULA Amazon 搜尋（整欄一個公式）。

env：
  COVER_UPDATER_DRY_RUN  1 = 只印不寫
  AUTO_FILL_BATCH_SIZE   單次最多更新幾列（預設 500、純 sheet 操作很快）
"""
import logging
import os
import time
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.oauth import load_creds
from utils.sheets import sheet_id

logger = logging.getLogger("library_cover_updater.auto_fill_links")


# 對應 autoFillLibraryLinks.js 公式模板
# $A2 用 string format、之後 replace 成實際 row
FORMULA_TEMPLATES = {
    10: '=IF(ISBLANK($A{R}), "", HYPERLINK("https://taiwanlibrarysearch.herokuapp.com/?book=" & $A{R}, "⚡ 搜全台電子書"))',  # J
    11: '=IF(ISBLANK($A{R}), "", HYPERLINK("https://reading.udn.com/udnlib/tpc/search?keyword=" & $A{R}, "📰 查新北UDN"))',  # K
    12: '=IF(ISBLANK($A{R}), "", HYPERLINK("https://reading.udn.com/udnlib/tpml/search?keyword=" & $A{R}, "📰 查北市UDN"))',  # L
    13: '=IF(ISBLANK($A{R}), "", HYPERLINK("https://www.pubu.com.tw/search?q=" & $A{R}, "📱 搜Pubu"))',  # M
    14: '=IF(ISBLANK($A{R}), "", HYPERLINK("https://www.google.com/search?q=Lexile+" & $A{R}, "📈 搜Google Lexile"))',  # N
    15: '=IF(ISBLANK($A{R}), "", HYPERLINK("https://bookwizard.scholastic.com/search?search=1&filters=&prefilter=&text=" & $A{R}, "🎓 查年級"))',  # O
}

P_FORMULA = '=ARRAYFORMULA(IF(ISBLANK(A2:A), "", HYPERLINK("https://www.amazon.com/s?k=" & A2:A, "📦 搜Amazon等級")))'

# 對應 column letter（從 1-based index 換算）
COL_LETTERS = {10: "J", 11: "K", 12: "L", 13: "M", 14: "N", 15: "O", 16: "P"}


def run():
    dry_run = os.environ.get("COVER_UPDATER_DRY_RUN", "").lower() in ("1", "true", "yes")
    batch_size = int(os.environ.get("AUTO_FILL_BATCH_SIZE", "500"))

    creds = load_creds()
    sheets_service = build("sheets", "v4", credentials=creds)
    sid = sheet_id()

    logger.info("[auto_fill_links] Loading sheet (values + formulas)...")
    # 一次抓 values（看 A 欄書名）+ formulas（看 J-P 是否已有公式）
    res_v = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range="'ALL'!A:P",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    res_f = sheets_service.spreadsheets().values().get(
        spreadsheetId=sid, range="'ALL'!A:P",
        valueRenderOption="FORMULA",
    ).execute()
    values = res_v.get("values", [])
    formulas = res_f.get("values", [])

    logger.info(f"[auto_fill_links] Sheet rows: {len(values)} (dry_run={dry_run})")

    updates = []  # batch_update 用，list of {range, values}
    stats = Counter()

    # data row 從 index 1（Row 2）開始
    for i in range(1, len(values)):
        row_num = i + 1
        row_v = values[i] if i < len(values) else []
        row_f = formulas[i] if i < len(formulas) else []
        # pad
        while len(row_v) < 16:
            row_v.append("")
        while len(row_f) < 16:
            row_f.append("")

        title = str(row_v[0]).strip()
        if not title:
            continue

        # J-O 各欄獨立公式：empty cell（無值無公式）才填
        for col_idx, template in FORMULA_TEMPLATES.items():
            has_value = str(row_v[col_idx - 1]).strip() != ""
            has_formula = str(row_f[col_idx - 1]).strip() != ""
            if has_value or has_formula:
                continue
            formula = template.replace("{R}", str(row_num))
            updates.append({
                "range": f"'ALL'!{COL_LETTERS[col_idx]}{row_num}",
                "values": [[formula]],
            })
            stats[f"col_{COL_LETTERS[col_idx]}"] += 1

        if len(updates) >= batch_size:
            break  # 超過 batch size 留下次跑

    # P 欄 ARRAYFORMULA 只在 Row 2 設一次（如果還沒設）
    if len(formulas) > 1 and len(formulas[1]) >= 16:
        p2 = str(formulas[1][15]).strip()
        if not p2:
            updates.append({
                "range": "'ALL'!P2",
                "values": [[P_FORMULA]],
            })
            stats["col_P_arrayformula"] += 1

    logger.info(f"[auto_fill_links] Updates to apply: {len(updates)} (dry_run={dry_run})")

    if not updates:
        return {"task": "auto_fill_links", "targets": 0, "note": "no_new_books"}

    if not dry_run:
        try:
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": updates,
                },
            ).execute()
        except HttpError as e:
            stats[f"batchUpdate_err_{e.resp.status}"] += 1
            logger.error(f"[auto_fill_links] batchUpdate err: {e}")

    logger.info(f"[auto_fill_links] 完成 stats={dict(stats)}")
    return {
        "task": "auto_fill_links",
        "targets": len(updates),
        "elapsed_sec": 0,
        "stats": dict(stats),
    }
