"""Fail-closed evidence gates shared by duplicate detection and merging."""
from __future__ import annotations

import re
from typing import Any, Iterable

from utils.sheets import extract_file_id


COL_NOTE = 8
COL_MARKER = 19
COL_DRIVE_LINK = 20
COL_FILE_ID = 21
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


def _cell(row: list[Any], index: int) -> str:
    if index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def file_id_from_row(row: list[Any]) -> str:
    return _cell(row, COL_FILE_ID) or (
        extract_file_id(_cell(row, COL_DRIVE_LINK)) or ""
    )


def collect_file_ids(groups: Iterable[dict[str, Any]]) -> set[str]:
    return {
        file_id_from_row(row)
        for group in groups
        for row in group.get("rows", [])
        if file_id_from_row(row)
    }


def fetch_drive_metadata(drive_service, groups):
    """Fetch identity metadata in batches; failures remain explicit evidence."""
    result = {}

    def callback(request_id, response, exception):
        if exception is not None:
            result[request_id] = {"error": f"{type(exception).__name__}: {exception}"}
        else:
            result[request_id] = response

    file_ids = sorted(collect_file_ids(groups))
    for offset in range(0, len(file_ids), 100):
        batch = drive_service.new_batch_http_request(callback=callback)
        for file_id in file_ids[offset:offset + 100]:
            batch.add(
                drive_service.files().get(
                    fileId=file_id,
                    fields="id,name,size,md5Checksum,parents,trashed,mimeType",
                    supportsAllDrives=True,
                ),
                request_id=file_id,
            )
        batch.execute()
    return result


def assess_group(group, file_meta):
    """AUTO only when all rows prove identical Drive bytes/file identity."""
    assessed = dict(group)
    rows = group.get("rows", [])
    file_ids = [file_id_from_row(row) for row in rows]

    if rows and all(file_ids) and len(set(file_ids)) == 1:
        assessed.update(
            auto_merge=True,
            safety_class="same_file_id",
            safety_reason="all rows reference the same Drive fileId",
        )
        return assessed

    metadata = [file_meta.get(file_id, {}) for file_id in file_ids]
    md5_values = [str(meta.get("md5Checksum", "")) for meta in metadata]
    sizes = [int(meta.get("size", 0) or 0) for meta in metadata]
    all_reachable = (
        bool(rows)
        and all(file_ids)
        and all(meta and not meta.get("error") and not meta.get("trashed") for meta in metadata)
    )
    same_nonempty_md5 = (
        all_reachable
        and all(md5_values)
        and len(set(md5_values)) == 1
        and md5_values[0] != EMPTY_MD5
        and all(size > 0 for size in sizes)
    )
    if same_nonempty_md5:
        assessed.update(
            auto_merge=True,
            safety_class="same_md5",
            safety_reason="all Drive files have the same non-empty MD5",
        )
        return assessed

    if any(meta.get("error") for meta in metadata) or not all(file_ids):
        safety_class = "missing_or_stale_drive_evidence"
    elif any(md5 == EMPTY_MD5 or size == 0 for md5, size in zip(md5_values, sizes)):
        safety_class = "zero_byte_or_invalid_file"
    elif len(set(md5_values)) > 1:
        safety_class = "different_content"
    else:
        safety_class = "unproven_identity"
    assessed.update(
        auto_merge=False,
        safety_class=safety_class,
        safety_reason="title/author match is not file identity",
    )
    return assessed


def assess_groups(groups, file_meta):
    assessed = [assess_group(group, file_meta) for group in groups]
    return {
        "auto_mergeable": [group for group in assessed if group["auto_merge"]],
        "manual_review": [group for group in assessed if not group["auto_merge"]],
    }


def _extension_from_row(row, meta):
    name = str(meta.get("name", ""))
    if not name:
        note = _cell(row, COL_NOTE)
        match = re.search(r"(?:原始檔名|Original filename)\s*[:：]\s*([^；\r\n]+)", note)
        name = match.group(1).strip() if match else ""
    match = re.search(r"\.([A-Za-z0-9]{1,6})$", name)
    return match.group(1).lower() if match else ""


def master_score(row_number, row, file_meta):
    """Prefer a healthy useful asset; older row is only the tie-breaker."""
    file_id = file_id_from_row(row)
    meta = file_meta.get(file_id, {})
    score = 0
    if file_id:
        score += 20
    if meta and not meta.get("error") and not meta.get("trashed"):
        score += 100
    if int(meta.get("size", 0) or 0) > 0:
        score += 20
    if _cell(row, COL_MARKER).upper() != "DRIVE_GONE":
        score += 15
    note = _cell(row, COL_NOTE).lower()
    if not any(token in note for token in ("_duplicates_quarantine", "_archive_originals", "_conflict")):
        score += 10
    extension = _extension_from_row(row, meta)
    score += {"epub": 12, "pdf": 10, "azw3": 8, "mobi": 6}.get(extension, 0)
    score += sum(1 for index in range(1, min(len(row), 20)) if _cell(row, index))
    return score, -row_number


def choose_master(group, file_meta):
    items = list(zip(group["row_numbers"], group["rows"]))
    if not items:
        raise ValueError("duplicate group has no rows")
    return max(items, key=lambda item: master_score(item[0], item[1], file_meta))
