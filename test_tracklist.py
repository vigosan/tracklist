import hashlib
import tempfile
import unittest
from pathlib import Path

import tracklist


def sha(value, timestamp):
    return hashlib.sha1(
        f"{timestamp} {value} https://studio.youtube.com".encode()
    ).hexdigest()


class BuildAuthorizationTest(unittest.TestCase):
    def test_binds_each_cookie_variant_to_timestamp_and_origin(self):
        cookies = {
            "SAPISID": "abc",
            "__Secure-1PAPISID": "def",
            "__Secure-3PAPISID": "ghi",
        }
        auth = tracklist.build_authorization(cookies, 1000)
        self.assertEqual(
            auth,
            f"SAPISIDHASH 1000_{sha('abc', 1000)} "
            f"SAPISID1PHASH 1000_{sha('def', 1000)} "
            f"SAPISID3PHASH 1000_{sha('ghi', 1000)}",
        )


NETSCAPE = """\
# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tabc
studio.youtube.com\tFALSE\t/\tTRUE\t0\tSESSION\txyz
.google.com\tTRUE\t/\tTRUE\t0\tNID\tnope
"""


class ParseCookieFileTest(unittest.TestCase):
    def test_keeps_youtube_cookies_and_drops_other_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(NETSCAPE)
            cookies = tracklist.parse_cookie_file(path)
        self.assertEqual(cookies, {"SAPISID": "abc", "SESSION": "xyz"})


class RefreshHeadersTest(unittest.TestCase):
    def test_replaces_auth_and_cookie_but_preserves_the_rest(self):
        stored = {
            "cookie": "SAPISID=old",
            "authorization": "SAPISIDHASH 1_dead",
            "user-agent": "Mozilla/5.0",
        }
        cookies = {
            "SAPISID": "abc",
            "__Secure-1PAPISID": "def",
            "__Secure-3PAPISID": "ghi",
        }
        headers = tracklist.refresh_headers(stored, cookies, 1000)
        self.assertEqual(headers["cookie"], "SAPISID=abc; __Secure-1PAPISID=def; __Secure-3PAPISID=ghi")
        self.assertEqual(headers["authorization"], tracklist.build_authorization(cookies, 1000))
        self.assertEqual(headers["user-agent"], "Mozilla/5.0")
        self.assertEqual(stored["cookie"], "SAPISID=old")


class PlanSamplesTest(unittest.TestCase):
    def test_samples_every_interval_while_they_fit(self):
        self.assertEqual(tracklist.plan_samples(0, 90), [15, 75])
        self.assertEqual(tracklist.plan_samples(120, 210), [135, 195])

    def test_sample_that_does_not_fit_is_dropped(self):
        self.assertEqual(tracklist.plan_samples(0, 84), [15])
        self.assertEqual(tracklist.plan_samples(0, 24), [])


class MergeSamplesTest(unittest.TestCase):
    def test_consecutive_matches_collapse_into_one_suggestion(self):
        first = {"artist": "Kate Project", "title": "A Better World"}
        second = {"artist": "Wand", "title": "Happiness"}
        samples = [(15, first), (75, first), (135, second)]
        self.assertEqual(
            tracklist.merge_samples(samples),
            [
                {"artist": "Kate Project", "title": "A Better World", "label": "", "start": 15, "duration": 70, "suggested": True},
                {"artist": "Wand", "title": "Happiness", "label": "", "start": 135, "duration": 10, "suggested": True},
            ],
        )

    def test_missing_result_breaks_a_run(self):
        match = {"artist": "X", "title": "Y"}
        samples = [(15, match), (75, None), (135, match)]
        merged = tracklist.merge_samples(samples)
        self.assertEqual(
            [(s["start"], s["duration"]) for s in merged],
            [(15, 10), (135, 10)],
        )


class RenderSuggestionsTest(unittest.TestCase):
    def test_marks_suggested_entries_and_counts_them_separately(self):
        entries = [
            {"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60},
            {"artist": "B", "title": "U", "label": "", "start": 60, "duration": 30, "suggested": True},
        ]
        text = tracklist.render(entries, [], "video123456", False)
        self.assertIn("  - 00:00 A - T\n", text)
        self.assertIn("  - 01:00 B - U (?)", text)
        self.assertIn("1 identificados, 1 sugeridos, 0 huecos", text)

    def test_summary_omits_suggestions_when_none(self):
        entries = [{"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}]
        text = tracklist.render(entries, [], "video123456", False)
        self.assertIn("1 identificados, 0 huecos", text)
        self.assertNotIn("sugeridos", text)


if __name__ == "__main__":
    unittest.main()
