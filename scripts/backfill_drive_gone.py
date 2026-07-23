"""
一次性處理 DRIVE_GONE（2026-06-26）：先回補、找不到的搬封存分頁再刪。

策略（Gladys 拍板 2026-06-26）：
  1. 對每列 DRIVE_GONE，用『原始檔名(備註 I 欄)優先、否則書名』去『檔案清單_Export』
     活檔索引找還活著的同名檔（套 folder_sync 同一把守門：詞重疊≥0.34 且非同系列換集）。
     - 唯一命中           → re-link：改寫 U 欄為新 fileId URL + 清 T(DRIVE_GONE)。不動 A 欄書名。
     - 多個命中(ambiguous) → 確定性挑一個(精準檔名→最高 overlap→fileId 排序)後 re-link，記錄候選。
     - 命中的 fileId 已被別的 alive 列使用 → 視為重複,不 re-link,改進封存刪除(避免製造重複列)。
     - 找不到             → 搬到『_DriveGone封存』分頁(保留全列+時間+原因)再從 ALL 刪列。

預設 dry-run。設 env BACKFILL_APPLY=1 才真的寫 / 刪。
真寫前會把整份 ALL!A:U 備份成 scripts/_backup_ALL_<ts>.json。

讀取使用正式 runtime 的 Service Account；本腳本仍預設 dry-run。
"""
import io, json, os, re, sys, time, datetime
from collections import defaultdict, Counter
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from googleapiclient.discovery import build

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from utils.oauth import load_sa_creds

HERE = os.path.dirname(os.path.abspath(__file__))
APPLY = os.environ.get("BACKFILL_APPLY", "").lower() in ("1", "true", "yes")

# --- .env ---
env = {}
with open(os.path.join(REPO, ".env"), encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1); k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k] = v
SHEET_ID = env["LIBRARY_SHEET_ID"]
creds = load_sa_creds(env.get("GOOGLE_SA_JSON", ""))
sheets = build("sheets", "v4", credentials=creds)

COL_TITLE, COL_NOTE, COL_MARKER, COL_LINK = 0, 8, 19, 20
_ORIG_RE = re.compile(r"原始檔名[:：]\s*(.+)")
_WORD_RE = re.compile(r"[^0-9a-z一-鿿]+")
_NUM_RE = re.compile(r"\d+")
_ZLIB_RE = re.compile(r"\(z-?library\)", re.I)

def strip_ext(s): return re.sub(r"\.[^.]*$", "", s or "").strip()
def norm_key(s):
    s = re.sub(r"\.[^.]*$", "", (s or "").lower())
    return re.sub(r"[^0-9a-z一-鿿]+", "", s)
def word_set(s):
    if not s: return set()
    x = _ZLIB_RE.sub(" ", str(s).lower()); x = _WORD_RE.sub(" ", x)
    return {w for w in x.split() if w and not w.isdigit()}
def overlap(a, b):
    sa, sb = word_set(a), word_set(b)
    if not sa: return 1.0
    return len(sa & sb) / len(sa)
def num_set(s): return {int(n) for n in _NUM_RE.findall(str(s or ""))}
def series_swap(new, ref):
    nn, nr = num_set(new), num_set(ref)
    if not nn or not nr or nn == nr: return False
    wn, wr = word_set(new), word_set(ref)
    if not wn or not wr: return False
    sh = len(wn & wr)
    return sh >= 2 and sh / min(len(wn), len(wr)) >= 0.5
def fid_of(s):
    if not s: return None
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", s) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", s)
    return m.group(1) if m else None
def orig_name(note):
    m = _ORIG_RE.search(note or "")
    return m.group(1).split("；")[0].split(" ; ")[0].strip() if m else ""

# --- ALL sheet + gid ---
meta = sheets.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
all_gid = None
have_archive = False
for s in meta["sheets"]:
    t = s["properties"]["title"]
    if t == "ALL": all_gid = s["properties"]["sheetId"]
    if t == "_DriveGone封存": have_archive = True
if all_gid is None:
    raise RuntimeError("找不到 ALL 分頁")

rows = sheets.spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range="'ALL'!A2:U", valueRenderOption="FORMULA",
).execute().get("values", [])

# alive 列已用的 fileId（避免回補製造重複）
alive_fids = set()
for row in rows:
    while len(row) <= COL_LINK: row.append("")
    if str(row[COL_MARKER]).strip() == "DRIVE_GONE":
        continue
    f = fid_of(str(row[COL_LINK]))
    if f: alive_fids.add(f)

# 活檔索引 key -> [(fid, filename)]
idx = sheets.spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range="'檔案清單_Export'!A2:E").execute().get("values", [])
key2files = defaultdict(list)
for r in idx:
    name = r[0] if len(r) > 0 else ""
    fid = r[1] if len(r) > 1 else ""
    if name and fid:
        key2files[norm_key(name)].append((fid, name))

# --- 分類 ---
relink = []        # (row_num, new_fid, new_name, ref, n_cands)
archive_del = []   # (row_num, ref, reason)
plan_log = []
for idx0, row in enumerate(rows, start=2):
    while len(row) <= COL_LINK: row.append("")
    if str(row[COL_MARKER]).strip() != "DRIVE_GONE":
        continue
    title = str(row[COL_TITLE]).strip()
    onm = orig_name(str(row[COL_NOTE]))
    ref = onm or title
    ref_s = strip_ext(ref)
    cands = key2files.get(norm_key(ref_s), [])
    good = [(fid, nm) for fid, nm in cands
            if overlap(strip_ext(nm), ref_s) >= 0.34 and not series_swap(strip_ext(nm), ref_s)]
    if not good:
        reason = "no_match" + ("(no_origname)" if not onm else "")
        archive_del.append((idx0, ref, reason))
        plan_log.append({"row": idx0, "action": "archive_delete", "ref": ref, "reason": reason})
        continue
    # 確定性挑選：精準檔名相等 > 最高 overlap > fileId 字典序
    good.sort(key=lambda x: (strip_ext(x[1]).lower() != ref_s.lower(),
                             -overlap(strip_ext(x[1]), ref_s), x[0]))
    new_fid, new_name = good[0]
    if new_fid in alive_fids:
        archive_del.append((idx0, ref, "dup_in_catalog(已有別列指同檔)"))
        plan_log.append({"row": idx0, "action": "archive_delete", "ref": ref,
                         "reason": "dup_in_catalog", "would_link": new_fid})
        continue
    relink.append((idx0, new_fid, new_name, ref, len(good)))
    plan_log.append({"row": idx0, "action": "relink", "ref": ref,
                     "new_fid": new_fid, "new_name": new_name,
                     "n_cands": len(good), "ambiguous": len(good) > 1})

print(f"DRIVE_GONE 待處理：{len(relink)+len(archive_del)}")
print(f"  → re-link 回補       ：{len(relink)}  (其中 ambiguous 自動挑選 {sum(1 for r in relink if r[4]>1)})")
print(f"  → 搬封存後刪列        ：{len(archive_del)}")
c = Counter(r[2] for r in archive_del)
for k, v in c.most_common():
    print(f"       {k}: {v}")

with open(os.path.join(HERE, "_drive_gone_plan.json"), "w", encoding="utf-8") as f:
    json.dump(plan_log, f, ensure_ascii=False, indent=2)
print(f"\n逐列計畫已寫：scripts/_drive_gone_plan.json  (APPLY={APPLY})")

if not APPLY:
    print("\n== DRY RUN：未寫入任何東西。確認後設 BACKFILL_APPLY=1 再跑。==")
    sys.exit(0)

# ============ APPLY ============
ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
# 0) 整份 ALL 備份
with open(os.path.join(HERE, f"_backup_ALL_{ts}.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False)
print(f"[backup] 已備份 ALL!A2:U → scripts/_backup_ALL_{ts}.json ({len(rows)} 列)")

# 1) re-link：改 U + 清 T
data = []
for row_num, new_fid, _nm, _ref, _n in relink:
    url = f"https://drive.google.com/file/d/{new_fid}/view?usp=drivesdk"
    data.append({"range": f"'ALL'!U{row_num}", "values": [[url]]})
    data.append({"range": f"'ALL'!T{row_num}", "values": [[""]]})
BATCH = 500
for i in range(0, len(data), BATCH):
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data[i:i+BATCH]},
    ).execute()
print(f"[relink] 完成 {len(relink)} 列 (U 改寫 + 清 DRIVE_GONE)")

# 2) 封存分頁
if not have_archive:
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "_DriveGone封存"}}}]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range="'_DriveGone封存'!A1",
        valueInputOption="RAW",
        body={"values": [["封存時間", "原ALL列號", "原因", "A書名..U(原列完整內容)"]]},
    ).execute()
arch_rows = []
for row_num, ref, reason in archive_del:
    orig = rows[row_num - 2]
    arch_rows.append([ts, row_num, reason] + [str(x) for x in orig])
if arch_rows:
    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range="'_DriveGone封存'!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": arch_rows},
    ).execute()
print(f"[archive] 已封存 {len(arch_rows)} 列 → _DriveGone封存 分頁")

# 3) 刪列（由大到小，避免位移）
del_rownums = sorted([r[0] for r in archive_del], reverse=True)
reqs = [{"deleteDimension": {"range": {
    "sheetId": all_gid, "dimension": "ROWS",
    "startIndex": rn - 1, "endIndex": rn}}} for rn in del_rownums]
for i in range(0, len(reqs), 100):
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID, body={"requests": reqs[i:i+100]}).execute()
print(f"[delete] 已從 ALL 刪除 {len(del_rownums)} 列")
print("\n== APPLY 完成 ==")
