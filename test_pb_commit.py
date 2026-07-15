import unittest
from unittest.mock import Mock, patch

from tasks import pb_commit


class PbCommitIdempotencyTest(unittest.TestCase):
    @patch("tasks.pb_commit.sheet_id", return_value="sheet-id")
    @patch("tasks.pb_commit.load_creds", return_value=object())
    @patch("tasks.pb_commit.build")
    def test_existing_file_id_is_marked_committed_without_append(
        self, build_mock, _load_creds, _sheet_id
    ):
        service = Mock()
        build_mock.return_value = service
        values = service.spreadsheets.return_value.values.return_value
        draft = [
            ["Title", "Author", "Lang", "Source", "Status", "FileId", "URL", "Path", "Created", "Action", "Original"],
            ["Book", "Author", "英文", "Google Drive", "已擁有", "file-1", "url", "folder", "", "APPROVED", "book.epub"],
        ]
        values.get.return_value.execute.side_effect = [
            {"values": draft},
            {"values": [["file-1"]]},
        ]
        values.batchUpdate.return_value.execute.return_value = {}

        with patch.dict(pb_commit.os.environ, {}, clear=True):
            result = pb_commit.run()

        self.assertEqual("already_present", result["note"])
        self.assertEqual(0, values.append.call_count)
        update_body = values.batchUpdate.call_args.kwargs["body"]
        self.assertEqual("'_PB_Draft'!J2", update_body["data"][0]["range"])
        self.assertEqual([["COMMITTED"]], update_body["data"][0]["values"])


if __name__ == "__main__":
    unittest.main()
