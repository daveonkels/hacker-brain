import concurrent.futures
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import collect_hn as hn


class CollectorTests(unittest.TestCase):
    def test_creation_window_has_exact_boundaries(self):
        self.assertFalse(hn.in_window({"time": 99}, 100, 200))
        self.assertTrue(hn.in_window({"time": 100}, 100, 200))
        self.assertTrue(hn.in_window({"time": 199}, 100, 200))
        self.assertFalse(hn.in_window({"time": 200}, 100, 200))
        self.assertFalse(hn.in_window({"deleted": True}, 100, 200))

    def test_collection_resume_and_lossless_summary(self):
        records = {
            1: {"id": 1, "type": "story", "time": 99},
            2: {"id": 2, "type": "comment", "time": 100, "parent": 1, "by": "a", "text": "long " * 1000 + "<p>é &amp; 🐍"},
            3: {"id": 3, "type": "comment", "time": 199, "parent": 2, "by": "b", "text": "reply"},
            4: {"id": 4, "type": "comment", "deleted": True},
            5: None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            chunks = folder / "chunks"
            chunks.mkdir()
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                with patch.object(hn, "fetch", side_effect=lambda route: records[int(route.split("/")[1])]) as fetch:
                    hn.collect_chunk(pool, chunks, 1, 5)
                    hn.collect_chunk(pool, chunks, 1, 5)
                    self.assertEqual(fetch.call_count, 5)
            report = hn.summarize(folder, {"start_timestamp": 100, "end_timestamp": 200, "hours": 100/3600, "max_id": 5, "scan_start_id": 1})
            self.assertTrue(report["coverage"]["all_scanned_ids_accounted_for"])
            self.assertEqual(report["items"], 2)
            self.assertEqual(report["distinct_parents_not_in_window"], 1)
            exported = [json.loads(line) for line in (folder / "items.ndjson").read_text().splitlines()]
            self.assertEqual(exported[0]["text"], records[2]["text"])
            self.assertEqual(report["coverage"]["null_count"], 1)
            self.assertEqual(report["coverage"]["items_without_timestamp"], 1)

    def test_failed_chunk_retries_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp, concurrent.futures.ThreadPoolExecutor(1) as pool:
            folder = pathlib.Path(tmp)
            with patch.object(hn, "fetch", side_effect=RuntimeError("temporary failure")):
                result = hn.collect_chunk(pool, folder, 1, 1)
                self.assertEqual(len(result["failed_ids"]), 1)
            with patch.object(hn, "fetch", return_value={"id": 1, "time": 100}):
                result = hn.collect_chunk(pool, folder, 1, 1)
                self.assertEqual(result["failed_ids"], {})


if __name__ == "__main__":
    unittest.main()
