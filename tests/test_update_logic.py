import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stella


class WhatsNewGateTest(unittest.TestCase):
    CHANGELOG = {"1.1.0": ["line a", "line b"]}

    def test_first_run_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew(None, "1.1.0", self.CHANGELOG))

    def test_upgrade_shows(self):
        self.assertTrue(stella.should_show_whatsnew("1.0.2", "1.1.0", self.CHANGELOG))

    def test_same_version_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew("1.1.0", "1.1.0", self.CHANGELOG))

    def test_no_changelog_entry_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew("1.0.2", "1.2.0", self.CHANGELOG))


if __name__ == "__main__":
    unittest.main()
