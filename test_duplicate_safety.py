import unittest

from tasks.duplicate_safety import EMPTY_MD5, assess_group, choose_master
from tasks.duplicate_detector import scan_duplicate_groups


def row(title, file_id, note="", marker=""):
    values = [""] * 22
    values[0] = title
    values[8] = note
    values[19] = marker
    values[20] = f"https://drive.google.com/file/d/{file_id}/view"
    values[21] = file_id
    return values


class DuplicateSafetyTest(unittest.TestCase):
    def test_same_title_author_without_drive_identity_is_manual(self):
        first = row("Book", "a")
        second = row("Book", "b")
        first[1] = second[1] = "Same Author"
        groups = scan_duplicate_groups([first, second], {})
        self.assertEqual(0, len(groups["auto_mergeable"]))
        self.assertEqual(1, len(groups["manual_review"]))

    def test_same_file_id_is_auto(self):
        group = {
            "rows": [row("Book", "same"), row("Book", "same")],
            "row_numbers": [2, 3],
        }
        assessed = assess_group(group, {})
        self.assertTrue(assessed["auto_merge"])
        self.assertEqual("same_file_id", assessed["safety_class"])

    def test_same_nonempty_md5_is_auto(self):
        group = {
            "rows": [row("Book", "a"), row("Book", "b")],
            "row_numbers": [2, 3],
        }
        meta = {
            "a": {"md5Checksum": "abc", "size": "10", "name": "a.epub"},
            "b": {"md5Checksum": "abc", "size": "10", "name": "b.epub"},
        }
        self.assertTrue(assess_group(group, meta)["auto_merge"])

    def test_zero_byte_or_different_content_is_manual(self):
        group = {
            "rows": [row("Book", "a"), row("Book", "b")],
            "row_numbers": [2, 3],
        }
        zero_meta = {
            "a": {"md5Checksum": EMPTY_MD5, "size": "0"},
            "b": {"md5Checksum": EMPTY_MD5, "size": "0"},
        }
        self.assertFalse(assess_group(group, zero_meta)["auto_merge"])
        different_meta = {
            "a": {"md5Checksum": "abc", "size": "10"},
            "b": {"md5Checksum": "xyz", "size": "10"},
        }
        self.assertFalse(assess_group(group, different_meta)["auto_merge"])

    def test_master_prefers_healthy_epub_over_old_drive_gone_row(self):
        group = {
            "rows": [
                row("Book", "old", "_duplicates_quarantine", "DRIVE_GONE"),
                row("Book", "new"),
            ],
            "row_numbers": [2, 99],
        }
        meta = {
            "old": {"error": "404", "name": "old.pdf"},
            "new": {"size": "100", "name": "new.epub", "trashed": False},
        }
        master_row, _ = choose_master(group, meta)
        self.assertEqual(99, master_row)


if __name__ == "__main__":
    unittest.main()
