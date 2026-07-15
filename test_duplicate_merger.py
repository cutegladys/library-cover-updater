import unittest
from unittest.mock import Mock

from tasks.duplicate_merger import merge_groups_batch


def make_group(title, row_numbers):
    rows = []
    for index, _ in enumerate(row_numbers):
        rows.append(
            [
                title,
                "author" if index == 0 else "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    return {"title": title, "row_numbers": row_numbers, "rows": rows}


class MergeGroupsBatchTest(unittest.TestCase):
    def test_many_groups_use_only_three_sheet_write_requests(self):
        service = Mock()
        spreadsheets = service.spreadsheets.return_value
        values = spreadsheets.values.return_value
        values.batchUpdate.return_value.execute.return_value = {}
        values.append.return_value.execute.return_value = {}
        spreadsheets.batchUpdate.return_value.execute.return_value = {}

        groups = [
            make_group(f"Book {index}", [100 + index, 200 + index])
            for index in range(38)
        ]
        results = merge_groups_batch(service, "sheet-id", 123, groups)

        self.assertEqual(38, len(results))
        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(1, values.batchUpdate.call_count)
        self.assertEqual(1, values.append.call_count)
        self.assertEqual(1, spreadsheets.batchUpdate.call_count)

        master_body = values.batchUpdate.call_args.kwargs["body"]
        self.assertEqual(38, len(master_body["data"]))
        backup_body = values.append.call_args.kwargs["body"]
        self.assertEqual(38, len(backup_body["values"]))
        delete_body = spreadsheets.batchUpdate.call_args.kwargs["body"]
        start_indexes = [
            request["deleteDimension"]["range"]["startIndex"]
            for request in delete_body["requests"]
        ]
        self.assertEqual(sorted(start_indexes, reverse=True), start_indexes)


if __name__ == "__main__":
    unittest.main()
