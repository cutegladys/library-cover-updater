"""共用 Drive 操作：下載檔案、上傳封面（經 relay）、檔名安全化、IMAGE 公式。"""
import os
import re


_safe_re = re.compile(r"[^\w\-. ]+", re.UNICODE)


def cover_folder_id() -> str:
    fid = os.environ.get("COVER_ART_FOLDER_ID", "").strip()
    if not fid:
        raise RuntimeError("缺少 env：COVER_ART_FOLDER_ID")
    return fid


def safe_name(s, max_len: int = 80) -> str:
    s = _safe_re.sub("_", s).strip()
    return s[:max_len] or "cover"


def get_metadata(drive_service, file_id: str):
    """打 Drive API 拿 file metadata；返 None 表 404 / err。"""
    return drive_service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,thumbnailLink,trashed,size",
        supportsAllDrives=True,
    ).execute()


def download_media(drive_service, file_id: str) -> bytes:
    return drive_service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()


def upload_cover(drive_service, name: str, content_bytes: bytes, mime: str) -> str:
    """上傳封面到 Cover Art 資料夾 + 設 anyone-with-link viewer。返 fileId。

    經 relay 以 owner 身分建檔（SA 無 My Drive 配額不能建檔；封面仍存在 owner 的
    Cover Art 夾、擁有者是 owner、與既有封面一致、不改外部 lh3 網址）。
    `drive_service` 參數保留以維持呼叫端介面相容，實際不再使用。
    """
    import base64

    from utils.relay import relay_call

    b64 = base64.b64encode(content_bytes).decode("ascii")
    res = relay_call("drive.upload", {
        "folderId": cover_folder_id(),
        "name": name,
        "mimeType": mime,
        "contentBase64": b64,
    })
    return res["fileId"]


def image_formula_for_drive_file(file_id: str) -> str:
    """直連 Drive 檔當圖片（image/* mime 或上傳到 Cover folder 的圖）。

    用 lh3.googleusercontent.com/d/<id>=w600 — 直接回 image、無 redirect。
    之前用 drive.google.com/uc?export=view 會 303 redirect 到 drive.usercontent，
    Telegram link-preview fetcher 不跟 → bot 顯示無封面（2026-05-27 修正）。
    """
    return f'=IMAGE("https://lh3.googleusercontent.com/d/{file_id}=w600")'


def image_formula_for_thumbnail(thumbnail_link: str) -> str:
    """Drive thumbnailLink fallback（audio/video/特殊 mime）。"""
    safe = thumbnail_link.replace('"', "")
    return f'=IMAGE("{safe}")'
