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

    def test_non_string_tags_skipped(self):
        state = {}
        stella.set_tags(state, "u1", ["ok", None, 42, "good"])
        self.assertEqual(stella.get_tags(state, "u1"), ["ok", "good"])

    def test_resume_roundtrip(self):
        state = {}
        self.assertIsNone(stella.get_resume(state))
        stella.set_resume(state, {"slug": "rrn_com_tr", "cursor": 4})
        self.assertEqual(stella.get_resume(state)["cursor"], 4)
        stella.clear_resume(state)
        self.assertIsNone(stella.get_resume(state))


from datetime import datetime


class FilterSpecRoundTripTest(unittest.TestCase):
    def test_full_roundtrip(self):
        spec = stella.FilterSpec(
            title_words=["merkel"], title_mode="all",
            text_words=["steuer", "reform"], text_mode="any",
            date_from=datetime(2026, 1, 2),
            date_to=datetime(2026, 3, 4, 23, 59, 59),
            site_slugs=["rrn_com_tr"], tags=["politics"])
        back = stella.FilterSpec.from_dict(spec.to_dict())
        self.assertEqual(back.title_words, ["merkel"])
        self.assertEqual(back.title_mode, "all")
        self.assertEqual(back.text_words, ["steuer", "reform"])
        self.assertEqual(back.date_from, datetime(2026, 1, 2))
        self.assertEqual(back.date_to, datetime(2026, 3, 4, 23, 59, 59))
        self.assertEqual(back.site_slugs, ["rrn_com_tr"])
        self.assertEqual(back.tags, ["politics"])

    def test_empty_dates_roundtrip(self):
        back = stella.FilterSpec.from_dict(stella.FilterSpec().to_dict())
        self.assertIsNone(back.date_from)
        self.assertIsNone(back.date_to)


class FilterHistoryTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_empty_spec_not_recorded(self):
        state = {}
        stella.record_filter(state, stella.FilterSpec(site_slugs=["a", "b"]), self.path)
        self.assertEqual(stella.get_filter_history(state), [])

    def test_newest_first_and_dedup(self):
        state = {}
        a = stella.FilterSpec(tags=["a"])
        b = stella.FilterSpec(tags=["b"])
        stella.record_filter(state, a, self.path)
        stella.record_filter(state, b, self.path)
        stella.record_filter(state, a, self.path)  # re-apply bumps to front
        hist = stella.get_filter_history(state)
        self.assertEqual([h.tags for h in hist], [["a"], ["b"]])

    def test_capped_at_five(self):
        state = {}
        for i in range(8):
            stella.record_filter(state, stella.FilterSpec(title_words=[f"w{i}"]), self.path)
        hist = stella.get_filter_history(state)
        self.assertEqual(len(hist), 5)
        self.assertEqual(hist[0].title_words, ["w7"])  # newest

    def test_corrupt_entry_skipped(self):
        state = {"__meta__": {"filter_history": [{"tags": ["ok"]}, "garbage", 42]}}
        hist = stella.get_filter_history(state)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].tags, ["ok"])

    def test_persists_to_disk(self):
        state = {}
        stella.record_filter(state, stella.FilterSpec(tags=["x"]), self.path)
        reloaded = stella.load_state(self.path)
        self.assertEqual(stella.get_filter_history(reloaded)[0].tags, ["x"])


if __name__ == "__main__":
    unittest.main()
