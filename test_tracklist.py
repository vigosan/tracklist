import hashlib
import tempfile
import unittest
from pathlib import Path

import recognize_tui
import recognize_ui
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


class ParseVideoIdTest(unittest.TestCase):
    def test_extracts_id_from_every_supported_form(self):
        self.assertEqual(tracklist.parse_video_id("https://www.youtube.com/watch?v=_6r0Kd41jfs"), "_6r0Kd41jfs")
        self.assertEqual(tracklist.parse_video_id("https://youtu.be/_6r0Kd41jfs"), "_6r0Kd41jfs")
        self.assertEqual(tracklist.parse_video_id("https://studio.youtube.com/video/_6r0Kd41jfs/edit"), "_6r0Kd41jfs")
        self.assertEqual(tracklist.parse_video_id("_6r0Kd41jfs"), "_6r0Kd41jfs")


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


class PentestLineTest(unittest.TestCase):
    def test_formats_each_event_kind(self):
        self.assertEqual(recognize_ui.pentest_line(("gap", 1, 7, 0, 242)), "[*] hueco 1/7 (00:00-04:02)")
        self.assertEqual(
            recognize_ui.pentest_line(("sample", 75, {"artist": "Duran Duran", "title": "Too Late Marlene"})),
            "[+] 01:15 Duran Duran - Too Late Marlene",
        )
        self.assertEqual(recognize_ui.pentest_line(("sample", 135, None)), "[-] 02:15 sin match")
        self.assertEqual(recognize_ui.pentest_line(("backoff",)), "[!] rate limit - backoff 30s")
        self.assertEqual(recognize_ui.pentest_line(("done", 8, 552)), "[*] 8 sugerencias en 09:12")


ENTRY = {"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}
SUGGESTION = {"artist": "B", "title": "U", "label": "", "start": 75, "duration": 10, "suggested": True}
GAPS = [(60, 300), (400, 600)]


class TuiRowsTest(unittest.TestCase):
    def test_initial_state_shows_entries_and_pending_gaps(self):
        self.assertEqual(
            recognize_ui.tui_rows([ENTRY], GAPS, {}, None),
            [
                "[green]  - 00:00 A - T[/green]",
                "[dim]  - 01:00 ????? (sin identificar, hasta 05:00)[/dim]",
                "[dim]  - 06:40 ????? (sin identificar, hasta 10:00)[/dim]",
            ],
        )

    def test_active_gap_shows_progress_and_partial_matches(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [SUGGESTION], "backoff": False}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 buscando (muestra 2/4)...[/cyan]")
        self.assertEqual(rows[2], "[yellow]  - 01:15 B - U (?)[/yellow]")
        self.assertEqual(rows[3], "[dim]  - 06:40 ????? (sin identificar, hasta 10:00)[/dim]")

    def test_active_gap_before_first_sample_shows_download(self):
        active = {"index": 0, "sample": 0, "total": 4, "matches": [], "backoff": False}
        self.assertEqual(
            recognize_ui.tui_rows([ENTRY], GAPS, {}, active)[1],
            "[cyan]  - 01:00 descargando...[/cyan]",
        )

    def test_completed_gap_consolidates_suggestions_and_residuals(self):
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {0: [SUGGESTION]}, None)
        self.assertEqual(rows[1], "[yellow]  - 01:15 B - U (?)[/yellow]")
        self.assertEqual(rows[2], "[dim]  - 01:25 ????? (sin identificar, hasta 05:00)[/dim]")

    def test_completed_late_gap_adds_no_spurious_leading_residual(self):
        late = {"artist": "C", "title": "V", "label": "", "start": 415, "duration": 10, "suggested": True}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {1: [late]}, None)
        self.assertNotIn("[dim]  - 00:00 ????? (sin identificar, hasta 06:40)[/dim]", rows)
        self.assertIn("[yellow]  - 06:55 C - V (?)[/yellow]", rows)
        self.assertIn("[dim]  - 07:05 ????? (sin identificar, hasta 10:00)[/dim]", rows)

    def test_entry_line_escapes_rich_markup_in_metadata(self):
        entry = {"artist": "DJ [i]X[/i]", "title": "T [Extended]", "label": "", "start": 0, "duration": 60}
        self.assertEqual(
            recognize_ui.entry_line(entry),
            r"[green]  - 00:00 DJ \[i]X\[/i] - T \[Extended][/green]",
        )


STATE = {
    "gap": (3, 7),
    "sample": 6,
    "total": 12,
    "sample_started": 100.0,
    "backoff": False,
    "suggestions": 8,
    "began": 50.0,
}


class StatusFieldsTest(unittest.TestCase):
    def test_interpolates_progress_inside_the_current_sample(self):
        fields = recognize_tui.status_fields(STATE, 102.0)
        self.assertEqual(fields["gap"], "3/7")
        self.assertEqual(fields["sample"], "7/12")
        self.assertEqual(fields["percent"], 54)
        self.assertEqual(fields["suggestions"], 8)
        self.assertEqual(fields["elapsed"], "00:52")
        self.assertFalse(fields["backoff"])

    def test_progress_freezes_during_backoff(self):
        fields = recognize_tui.status_fields({**STATE, "backoff": True}, 160.0)
        self.assertEqual(fields["percent"], 50)
        self.assertTrue(fields["backoff"])

    def test_interpolation_never_reaches_the_next_sample(self):
        self.assertEqual(recognize_tui.status_fields(STATE, 999.0)["percent"], 57)

    def test_percent_never_exceeds_one_hundred(self):
        finished = {**STATE, "sample": 12}
        self.assertEqual(recognize_tui.status_fields(finished, 999.0)["percent"], 100)

    def test_before_the_first_gap(self):
        idle = {"gap": None, "sample": 0, "total": 0, "sample_started": 0.0,
                "backoff": False, "suggestions": 0, "began": 50.0}
        fields = recognize_tui.status_fields(idle, 50.0)
        self.assertEqual(fields["gap"], "-")
        self.assertEqual(fields["percent"], 0)
        self.assertEqual(fields["sample"], "-")
        self.assertEqual(fields["elapsed"], "00:00")


class PhaseAndStateTest(unittest.TestCase):
    def test_phase_labels(self):
        self.assertEqual(recognize_tui.phase_label("cookies"), "Leyendo cookies de Brave...")
        self.assertEqual(recognize_tui.phase_label("claims"), "Pidiendo reclamaciones a Studio...")
        self.assertEqual(recognize_tui.phase_label("video"), "Pidiendo datos del video...")
        self.assertEqual(recognize_tui.phase_label("recognizing"), "")
        self.assertEqual(recognize_tui.phase_label("cualquier-otra"), "")

    def test_initial_state_starts_empty(self):
        state = recognize_tui.initial_state(500.0)
        self.assertIsNone(state["gap"])
        self.assertEqual(state["sample"], 0)
        self.assertEqual(state["total"], 0)
        self.assertEqual(state["suggestions"], 0)
        self.assertFalse(state["backoff"])
        self.assertEqual(state["began"], 500.0)
        self.assertEqual(state["sample_started"], 500.0)
        self.assertEqual(state["phase"], "cookies")
        self.assertFalse(state["paused"])


class LimiterTest(unittest.TestCase):
    def test_starts_at_three_and_halves_on_throttle(self):
        limiter = tracklist.Limiter()
        self.assertEqual(limiter.workers, 3)
        limiter.throttle()
        self.assertEqual(limiter.workers, 1)
        limiter.throttle()
        self.assertEqual(limiter.workers, 1)

    def test_recovers_one_worker_after_enough_clean_samples(self):
        limiter = tracklist.Limiter()
        limiter.throttle()
        for _ in range(tracklist.RECOVER_AFTER - 1):
            limiter.reward()
        self.assertEqual(limiter.workers, 1)
        limiter.reward()
        self.assertEqual(limiter.workers, 2)

    def test_never_grows_beyond_the_maximum(self):
        limiter = tracklist.Limiter()
        for _ in range(tracklist.RECOVER_AFTER * 3):
            limiter.reward()
        self.assertEqual(limiter.workers, 3)

    def test_throttle_resets_the_recovery_counter(self):
        limiter = tracklist.Limiter()
        limiter.throttle()
        for _ in range(tracklist.RECOVER_AFTER - 1):
            limiter.reward()
        limiter.throttle()
        for _ in range(tracklist.RECOVER_AFTER - 1):
            limiter.reward()
        self.assertEqual(limiter.workers, 1)


if __name__ == "__main__":
    unittest.main()
