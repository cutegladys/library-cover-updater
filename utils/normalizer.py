"""
檔名標準化器 — 對應 normalizer.js（190 行 → ~90 行 Python）

normalize_filename(raw)         去掉副檔名 + 廣告 + 出版社標籤 + 全形→半形 + 系列標籤解析
normalize_author(raw)           去掉 (Z-Library) + 非法字元
build_filename(title, author, ext)  組「[作者] 書名.ext」
"""
import re
import unicodedata

NOISE_PATTERNS = [
    re.compile(r"\bZ-Library\b", re.I),
    re.compile(r"\(Z-Library\)", re.I),
    re.compile(r"z-lib\.org", re.I),
    re.compile(r"zlibrary", re.I),
    re.compile(r"booksc\.org", re.I),
    re.compile(r"b-ok\.(cc|org)", re.I),
    re.compile(r"libgen\.(is|rs|li)", re.I),
    re.compile(r"www\.\S+\.(com|net|org|io)\b", re.I),
    re.compile(r"https?://\S+", re.I),
    re.compile(r"@\w+"),
    re.compile(r"\[?掃描版\]?"),
    re.compile(r"\[?電子版\]?"),
    re.compile(r"\[?完整版\]?"),
    re.compile(r"\[?高清版\]?"),
    re.compile(r"\[?修訂版\]?"),
    re.compile(r"\(scan\)", re.I),
    re.compile(r"\(digital\)", re.I),
    re.compile(r"\(retail\)", re.I),
    re.compile(r"\bEPUB3?\b", re.I),
    re.compile(r"\bMOBI\b", re.I),
    re.compile(r"\bAZW3?\b", re.I),
]

PUBLISHER_TAG_PATTERNS = [
    re.compile(rf"\[{p}[^\]]*\]", re.I)
    for p in ("Scholastic", "Resource", "Oxford", "Penguin", "Harper", "Random House", "DK", "Disney", "ABC", "Audible")
]

ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def normalize_filename(raw: str) -> str:
    if not raw:
        return ""
    s = str(raw)

    # 步驟 9 前置：先去掉副檔名
    s = re.sub(r"\.[a-zA-Z0-9]{1,6}$", "", s)

    # 步驟 1：Unicode NFC
    s = unicodedata.normalize("NFC", s)

    # 步驟 2：全形 → 半形（數字 / 大小寫英文）
    s = _fullwidth_to_half(s)

    # 步驟 3：廣告雜訊
    for p in NOISE_PATTERNS:
        s = p.sub("", s)

    # 步驟 4：出版社標籤
    for p in PUBLISHER_TAG_PATTERNS:
        s = p.sub("", s)

    # 步驟 5：統一括號
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"[｛｝\{\}]", "", s)
    s = re.sub(r"\(\s*\)", "", s)

    # 步驟 6：壓縮連續空白
    s = re.sub(r"[\s　]+", " ", s).strip()

    # 步驟 8：系列標籤解析
    s = _parse_series_tag(s)

    # 移除 Drive 非法字元
    s = ILLEGAL_CHARS_RE.sub("", s)

    # 最後壓空白
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_author(raw: str) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    s = re.sub(r"\(Z-Library\)", "", s, flags=re.I).strip()
    s = re.sub(r"\s+", " ", s).strip()
    s = ILLEGAL_CHARS_RE.sub("", s)
    return s


def build_filename(title: str, author: str, ext: str) -> str:
    if not ext.startswith("."):
        ext = "." + ext
    base = f"[{author}] {title}" if author else title
    base = ILLEGAL_CHARS_RE.sub("", base)
    if len(base + ext) > 250:
        base = base[: 250 - len(ext)]
    return base + ext


def _parse_series_tag(s: str) -> str:
    m = re.match(r"^\[([^\]]+)\]\s*[•·\-]?\s*(.+)$", s)
    if m:
        series_part = m.group(1).strip()
        title_part = m.group(2).strip()
        publisher_re = re.compile(r"^(Scholastic|Oxford|Penguin|Harper|Random House|DK|Disney|ABC|Audible|Resource)", re.I)
        if not publisher_re.match(series_part):
            s = f"{series_part} - {title_part}"
    s = re.sub(r"\s*[•·]\s*", " - ", s)
    s = re.sub(r"^[\s\-]+", "", s).strip()
    return s


def _fullwidth_to_half(s: str) -> str:
    """全形數字 / 英文 → 半形（其他不動）"""
    result = []
    for c in s:
        code = ord(c)
        # FF10-FF19 全形 0-9 / FF21-FF3A 全形 A-Z / FF41-FF5A 全形 a-z
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(c)
    return "".join(result)
