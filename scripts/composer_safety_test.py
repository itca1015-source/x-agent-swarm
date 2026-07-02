import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chrome
import engage


class ComposerSafetyTest(unittest.TestCase):
    def test_detects_exact_double_paste(self):
        reply = (
            "Energy sector moves on policy timelines, not tech timelines. "
            "The real leverage isn't the keynote—it's whether anyone in that "
            "room can actually approve procurement decisions without 18 months "
            "of internal review."
        )

        self.assertTrue(chrome.composer_has_duplicate(reply + reply, reply))

    def test_detects_double_paste_with_line_break_between_copies(self):
        reply = "clean text once."

        self.assertTrue(chrome.composer_has_duplicate(reply + "\n" + reply, reply))

    def test_detects_repeated_opening_even_when_linkification_changes_boundary(self):
        reply = (
            "Energy sector moves on policy timelines, not tech timelines. "
            "The real leverage isn't the keynote—it's whether anyone in that "
            "room can actually approve procurement decisions without 18 months "
            "of internal review."
        )
        malformed = (
            "Energy sector moves on policy timelines, not tech timelines. "
            "The real leverage isn't the keynote—it's whether anyone in that "
            "room can actually approve procurement decisions without 18 months "
            "of internal http://review.Energy sector moves on policy timelines, "
            "not tech timelines. The real leverage isn't the keynote—it's "
            "whether anyone in that room can actually approve procurement "
            "decisions without 18 months of internal review."
        )

        self.assertTrue(chrome.composer_has_duplicate(malformed, reply))

    def test_detects_draft_that_repeats_itself_before_browser(self):
        reply = "This is long enough to be a real generated reply with useful content."

        self.assertTrue(chrome.text_repeats_itself(reply + reply))

    def test_does_not_flag_normal_long_draft(self):
        reply = (
            "This is long enough to be a real generated reply with useful content. "
            "It has one clear point and does not restart from the opening."
        )

        self.assertFalse(chrome.text_repeats_itself(reply))

    def test_does_not_flag_intended_text(self):
        reply = "clean text once."

        self.assertFalse(chrome.composer_has_duplicate(reply, reply))

    def test_normalization_handles_nbsp_and_crlf(self):
        self.assertEqual(
            chrome.normalize_composer_text("  hello\u00a0world\r\n"),
            "hello world",
        )

    def test_empty_enabled_composer_would_not_match_intended_reply(self):
        self.assertNotEqual(
            chrome.normalize_composer_text(""),
            chrome.normalize_composer_text("text that must be present before submit"),
        )

    def test_engage_imports_safety_helpers(self):
        self.assertIs(engage.normalize_composer_text, chrome.normalize_composer_text)


if __name__ == "__main__":
    unittest.main()
