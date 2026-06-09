import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stella


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)  # start with no file

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_missing_file_loads_empty(self):
        self.assertEqual(stella.load_state(self.path), {})

    def test_corrupt_file_loads_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(stella.load_state(self.path), {})

    def test_read_roundtrip(self):
        state = {}
        self.assertFalse(stella.is_read(state, "u1"))
        stella.set_read(state, "u1", True)
        self.assertTrue(stella.is_read(state, "u1"))
        stella.save_state(state, self.path)
        self.assertTrue(stella.is_read(stella.load_state(self.path), "u1"))

    def test_unread_with_no_tags_prunes_row(self):
        state = {}
        stella.set_read(state, "u1", True)
        stella.set_read(state, "u1", False)
        self.assertNotIn("u1", state)  # pruned when unread and untagged

    def test_tags_normalized_and_deduped(self):
        state = {}
        stella.set_tags(state, "u1", ["  Politics ", "politics", "EU", ""])
        self.assertEqual(stella.get_tags(state, "u1"), ["politics", "eu"])

    def test_empty_tags_prune_when_unread(self):
        state = {}
        stella.set_tags(state, "u1", ["x"])
        stella.set_tags(state, "u1", [])
        self.assertNotIn("u1", state)

    def test_empty_tags_kept_when_read(self):
        state = {}
        stella.set_read(state, "u1", True)
        stella.set_tags(state, "u1", [])
        self.assertIn("u1", state)
        self.assertEqual(stella.get_tags(state, "u1"), [])

    def test_all_tags_union_sorted_skips_meta(self):
        state = {"__meta__": {"last_seen_version": "1.1.0"}}
        stella.set_tags(state, "u1", ["b", "a"])
        stella.set_tags(state, "u2", ["a", "c"])
        self.assertEqual(stella.all_tags(state), ["a", "b", "c"])

    def test_meta_helpers(self):
        state = {}
        self.assertIsNone(stella.get_last_seen_version(state))
        stella.set_last_seen_version(state, "1.1.0")
        self.assertEqual(stella.get_last_seen_version(state), "1.1.0")
        self.assertNotIn("__meta__", stella.all_tags(state))

    def test_resume_roundtrip(self):
        state = {}
        self.assertIsNone(stella.get_resume(state))
        stella.set_resume(state, {"slug": "rrn_com_tr", "cursor": 4})
        self.assertEqual(stella.get_resume(state)["cursor"], 4)
        stella.clear_resume(state)
        self.assertIsNone(stella.get_resume(state))


if __name__ == "__main__":
    unittest.main()
