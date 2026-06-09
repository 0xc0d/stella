import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stella


class WhatsNewGateTest(unittest.TestCase):
    CHANGELOG = {"1.1.0": ["line a", "line b"]}

    def test_fresh_install_does_not_show(self):
        # no last_seen + not an existing user (no prior data) → silent
        self.assertFalse(stella.should_show_whatsnew(None, "1.1.0", self.CHANGELOG,
                                                     existing_user=False))

    def test_returning_user_no_last_seen_shows(self):
        # upgrading from a pre-tracking version: no last_seen but has prior data
        self.assertTrue(stella.should_show_whatsnew(None, "1.1.0", self.CHANGELOG,
                                                    existing_user=True))

    def test_upgrade_shows(self):
        self.assertTrue(stella.should_show_whatsnew("1.0.2", "1.1.0", self.CHANGELOG))

    def test_same_version_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew("1.1.0", "1.1.0", self.CHANGELOG))

    def test_no_changelog_entry_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew("1.0.2", "1.2.0", self.CHANGELOG))
        # even a returning user sees nothing if there are no notes for this version
        self.assertFalse(stella.should_show_whatsnew(None, "1.2.0", self.CHANGELOG,
                                                     existing_user=True))


from datetime import datetime


class FilterSerializeTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(stella._serialize_filter(None, None), None)
        self.assertEqual(stella._deserialize_filter(None, None), (None, None))

    def test_month(self):
        v = stella._serialize_filter("month", (2026, 5))
        self.assertEqual(v, [2026, 5])
        self.assertEqual(stella._deserialize_filter("month", v), ("month", (2026, 5)))

    def test_day(self):
        d = datetime(2026, 5, 6)
        v = stella._serialize_filter("day", d)
        self.assertEqual(stella._deserialize_filter("day", v)[1].date(), d.date())

    def test_tag(self):
        v = stella._serialize_filter("tag", "politics")
        self.assertEqual(v, "politics")
        self.assertEqual(stella._deserialize_filter("tag", v), ("tag", "politics"))


class RawUrlTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("STELLA_UPDATE_BASE", None)

    def test_default_is_github(self):
        os.environ.pop("STELLA_UPDATE_BASE", None)
        self.assertIn("raw.githubusercontent.com", stella._raw_url("stella.py"))

    def test_env_override(self):
        os.environ["STELLA_UPDATE_BASE"] = "http://localhost:8000/x/"
        self.assertEqual(stella._raw_url("stella.py"),
                         "http://localhost:8000/x/stella.py")


if __name__ == "__main__":
    unittest.main()
