import unittest

from app import (
    build_v6_report, detect_v6_long_pauses, detect_v6_retries,
    v6_keep_segments,
)


def word(text, start, end, confidence):
    return {"word": text, "start": start, "end": end, "conf": confidence}


class SmartSpeechV6DetectionTests(unittest.TestCase):
    def test_low_confidence_phrase_restart_proposes_earlier_take(self):
        words = [
            word("сегодня", 1.0, 1.5, 0.31),
            word("погода", 1.55, 2.0, 0.42),
            word("сегодня", 2.3, 2.8, 0.91),
            word("погода", 2.85, 3.3, 0.94),
        ]
        cuts = detect_v6_retries(words, 10)
        self.assertEqual(len(cuts), 1)
        self.assertEqual((cuts[0]["start"], cuts[0]["end"]), (1.0, 2.0))

    def test_clear_intentional_repeated_phrase_is_kept(self):
        words = [
            word("очень", 1.0, 1.2, 0.92), word("хорошо", 1.2, 1.6, 0.91),
            word("очень", 1.8, 2.0, 0.93), word("хорошо", 2.0, 2.4, 0.92),
        ]
        self.assertEqual(detect_v6_retries(words, 10), [])

    def test_missing_confidence_never_authorizes_deletion(self):
        words = [
            {"word": "сегодня", "start": 1.0, "end": 1.5},
            {"word": "погода", "start": 1.55, "end": 2.0},
            {"word": "сегодня", "start": 2.3, "end": 2.8},
            {"word": "погода", "start": 2.85, "end": 3.3},
        ]
        self.assertEqual(detect_v6_retries(words, 10), [])

    def test_repetition_after_long_gap_is_kept(self):
        words = [
            word("сегодня", 1, 1.4, .2), word("погода", 1.4, 1.8, .2),
            word("сегодня", 5, 5.4, .95), word("погода", 5.4, 5.8, .95),
        ]
        self.assertEqual(detect_v6_retries(words, 10), [])

    def test_only_confident_stutter_restart_is_proposed(self):
        words = [word("привет", 0.5, 0.7, .3), word("привет", .8, 1.0, .9)]
        cuts = detect_v6_retries(words, 3)
        self.assertEqual(len(cuts), 1)
        self.assertEqual(cuts[0]["reason"], "low-confidence stutter corrected by clearer retry")

    def test_short_function_word_repetition_is_preserved(self):
        words = [word("я", 0.5, 0.7, .2), word("я", .8, 1.0, .95)]
        self.assertEqual(detect_v6_retries(words, 3), [])

    def test_long_pause_keeps_padding_and_short_pause(self):
        cuts = detect_v6_long_pauses([(1, 1.8), (4, 7)], 10)
        self.assertEqual(len(cuts), 1)
        self.assertEqual((cuts[0]["start"], cuts[0]["end"]), (4.3, 6.7))

    def test_overlapping_or_uncertain_proposal_is_not_auto_applied(self):
        report = {
            "duration": 8,
            "proposed_deletions": [
                {"start": 1, "end": 3, "automatic": False},
                {"start": 4, "end": 5, "automatic": True},
            ],
        }
        self.assertEqual(v6_keep_segments(report), [(0.0, 4), (5, 8)])

    def test_automatic_edits_are_capped_for_free_instance_rendering(self):
        report = build_v6_report([], [(i * 3, i * 3 + 2) for i in range(7)], 30)
        self.assertEqual(sum(item["automatic"] for item in report["proposed_deletions"]), 5)


if __name__ == "__main__":
    unittest.main()
