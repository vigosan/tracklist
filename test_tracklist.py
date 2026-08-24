import asyncio
import contextlib
import hashlib
import io
import importlib.util
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


class BaseTitleTest(unittest.TestCase):
    def test_a_parenthesised_remix_reduces_to_the_plain_title(self):
        self.assertEqual(tracklist.base_title("Try (MICHAEL WOODS REMIX)"), "try")
        self.assertEqual(tracklist.base_title("Try"), "try")

    def test_a_dashed_mix_reduces_to_the_plain_title(self):
        self.assertEqual(tracklist.base_title("Secret - Extended Vocal Mix"), "secret")

    def test_a_title_without_decoration_is_only_normalised(self):
        self.assertEqual(tracklist.base_title("  Night Train  "), "night train")

    def test_a_title_that_is_only_decoration_keeps_its_text(self):
        self.assertEqual(tracklist.base_title("(Reprise)"), "(reprise)")


class SameTrackTest(unittest.TestCase):
    def test_the_same_recording_under_two_titles_is_one_track(self):
        self.assertTrue(tracklist.same_track(
            {"artist": "Ian Van Dahl", "title": "Try (MICHAEL WOODS REMIX)"},
            {"artist": "Ian Van Dahl", "title": "Try"},
        ))

    def test_different_artists_are_never_the_same_track(self):
        self.assertFalse(tracklist.same_track(
            {"artist": "Ian Van Dahl", "title": "Try"},
            {"artist": "Rebel Vision", "title": "Try"},
        ))

    def test_different_titles_by_one_artist_stay_separate(self):
        self.assertFalse(tracklist.same_track(
            {"artist": "YNEL", "title": "Arch in Ya Back"},
            {"artist": "YNEL", "title": "Night Train"},
        ))


class MergeVariantTitlesTest(unittest.TestCase):
    def test_consecutive_variants_of_one_track_collapse(self):
        remix = {"artist": "Ian Van Dahl", "title": "Try (MICHAEL WOODS REMIX)"}
        plain = {"artist": "Ian Van Dahl", "title": "Try"}
        merged = tracklist.merge_samples([(393, remix), (453, plain)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "Try (MICHAEL WOODS REMIX)")
        self.assertEqual(merged[0]["duration"], 70)

    def test_the_same_track_apart_in_time_stays_two_entries(self):
        match = {"artist": "X", "title": "Y"}
        merged = tracklist.merge_samples([(15, match), (75, None), (135, match)])
        self.assertEqual(len(merged), 2)


class DropClaimedSuggestionsTest(unittest.TestCase):
    def make(self, start, duration):
        return {"artist": "S", "title": "T", "label": "", "start": start,
                "duration": duration, "suggested": True}

    def test_a_suggestion_overlapping_a_claim_is_dropped(self):
        claimed = [{"artist": "Absolom", "title": "Secret - Extended Vocal Mix",
                    "label": "", "start": 3082, "duration": 300}]
        suggestion = self.make(3014, 10)
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [suggestion])

    def test_a_suggestion_inside_a_claim_is_dropped(self):
        claimed = [{"artist": "A", "title": "T", "label": "", "start": 100, "duration": 200}]
        self.assertEqual(tracklist.drop_claimed(claimed, [self.make(150, 10)]), [])

    def test_a_suggestion_clear_of_every_claim_survives(self):
        claimed = [{"artist": "A", "title": "T", "label": "", "start": 100, "duration": 100}]
        suggestion = self.make(400, 10)
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [suggestion])

    def test_a_suggestion_ending_exactly_where_a_claim_starts_survives(self):
        claimed = [{"artist": "A", "title": "T", "label": "", "start": 200, "duration": 100}]
        suggestion = self.make(190, 10)
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [suggestion])

    def test_with_no_claims_every_suggestion_survives(self):
        suggestions = [self.make(10, 10), self.make(100, 10)]
        self.assertEqual(tracklist.drop_claimed([], suggestions), suggestions)


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


class StatusTextTest(unittest.TestCase):
    def test_finished_run_reports_completion_instead_of_the_last_sample(self):
        state = {**STATE, "phase": "done", "paused": False}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Completado - 8 sugerencias",
        )

    def test_finished_run_with_no_suggestions_says_so(self):
        state = {**STATE, "phase": "done", "paused": False, "suggestions": 0}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Completado - sin sugerencias",
        )

    def test_a_single_suggestion_is_not_pluralised(self):
        state = {**STATE, "phase": "done", "paused": False, "suggestions": 1}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Completado - 1 sugerencia",
        )

    def test_pause_takes_precedence_over_everything(self):
        state = {**STATE, "phase": "done", "paused": True}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "PAUSA",
        )

    def test_running_state_still_reports_the_current_sample(self):
        state = {**STATE, "phase": "recognizing", "paused": False}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Muestra 7/12",
        )

    def test_loading_phases_keep_their_label(self):
        state = {**STATE, "phase": "claims", "paused": False}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Pidiendo reclamaciones a Studio...",
        )

    def test_backoff_is_reported_while_running(self):
        state = {**STATE, "phase": "recognizing", "paused": False, "backoff": True}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "rate limit - backoff",
        )


class FinishedProgressTest(unittest.TestCase):
    def test_a_finished_run_fills_the_bar_even_mid_sample(self):
        state = {**STATE, "phase": "done", "sample": 6, "total": 12}
        self.assertEqual(recognize_tui.status_fields(state, 102.0)["percent"], 100)


class FinishedElapsedTest(unittest.TestCase):
    def test_the_clock_stops_when_the_run_finishes(self):
        state = {**STATE, "phase": "done", "ended": 110.0}
        self.assertEqual(recognize_tui.status_fields(state, 110.0)["elapsed"], "01:00")
        self.assertEqual(recognize_tui.status_fields(state, 9999.0)["elapsed"], "01:00")

    def test_a_running_clock_still_advances(self):
        state = {**STATE, "phase": "recognizing", "ended": None}
        self.assertEqual(recognize_tui.status_fields(state, 110.0)["elapsed"], "01:00")
        self.assertEqual(recognize_tui.status_fields(state, 170.0)["elapsed"], "02:00")


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


class BackoffDelayTest(unittest.TestCase):
    def test_the_first_wait_is_short_instead_of_half_a_minute(self):
        self.assertEqual(tracklist.backoff_delay(0), tracklist.BACKOFF_START)
        self.assertLess(tracklist.backoff_delay(0), 30)

    def test_each_repeated_failure_waits_longer(self):
        self.assertEqual(tracklist.backoff_delay(1), tracklist.BACKOFF_START * 2)
        self.assertEqual(tracklist.backoff_delay(2), tracklist.BACKOFF_START * 4)

    def test_the_wait_never_grows_past_the_ceiling(self):
        self.assertEqual(tracklist.backoff_delay(99), tracklist.BACKOFF_CEILING)



class GateTest(unittest.IsolatedAsyncioTestCase):
    async def test_gate_admits_up_to_current_worker_count(self):
        limiter = tracklist.Limiter()
        gate = tracklist.Gate(limiter)
        await gate.acquire()
        await gate.acquire()
        await gate.acquire()
        self.assertEqual(gate.active, 3)
        gate.release()
        self.assertEqual(gate.active, 2)

    async def test_gate_blocks_when_the_limiter_throttles(self):
        limiter = tracklist.Limiter()
        gate = tracklist.Gate(limiter)
        await gate.acquire()
        limiter.throttle()
        blocked = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        gate.release()
        await asyncio.wait_for(blocked, timeout=1)
        self.assertEqual(gate.active, 1)


class IsolatedCache:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        original = (tracklist.CACHE_DIR, tracklist.AUDIO_DIR)
        tracklist.CACHE_DIR = root / "cache"
        tracklist.AUDIO_DIR = root / "audio"

        def restore():
            tracklist.CACHE_DIR, tracklist.AUDIO_DIR = original

        self.addCleanup(restore)


class CacheTest(IsolatedCache, unittest.TestCase):
    def test_missing_cache_reads_as_empty(self):
        cache = tracklist.load_cache("vid12345678")
        self.assertEqual(cache, {"samples": {}, "title": None, "duration": None})

    def test_saved_samples_survive_a_reload(self):
        match = {"artist": "A", "title": "T"}
        tracklist.save_sample("vid12345678", 15, match)
        tracklist.save_sample("vid12345678", 75, None)
        self.assertEqual(
            tracklist.load_cache("vid12345678")["samples"], {15: match, 75: None}
        )

    def test_saved_samples_keep_the_cached_video_details(self):
        tracklist.save_video_details("vid12345678", "Un set", 3600)
        tracklist.save_sample("vid12345678", 15, None)
        cache = tracklist.load_cache("vid12345678")
        self.assertEqual(cache["title"], "Un set")
        self.assertEqual(cache["duration"], 3600)

    def test_each_video_keeps_its_own_samples(self):
        tracklist.save_sample("vid12345678", 15, {"artist": "A", "title": "T"})
        self.assertEqual(tracklist.load_cache("otro1234567")["samples"], {})

    def test_clear_removes_samples_and_audio(self):
        tracklist.save_sample("vid12345678", 15, None)
        tracklist.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        audio = tracklist.audio_path("vid12345678")
        audio.write_bytes(b"mp3")
        tracklist.clear_cache("vid12345678")
        self.assertEqual(tracklist.load_cache("vid12345678")["samples"], {})
        self.assertFalse(audio.exists())

    def test_clear_on_a_video_never_analysed_is_harmless(self):
        tracklist.clear_cache("vid12345678")

    def test_corrupt_cache_reads_as_empty_instead_of_crashing(self):
        tracklist.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tracklist.cache_path("vid12345678").write_text("{no es json")
        self.assertEqual(tracklist.load_cache("vid12345678")["samples"], {})


class ArgumentsTest(unittest.TestCase):
    def test_analysis_is_resumed_unless_fresh_is_requested(self):
        self.assertFalse(tracklist.build_parser().parse_args(["vid12345678"]).fresh)
        self.assertTrue(tracklist.build_parser().parse_args(["vid12345678", "--fresh"]).fresh)

    def test_content_id_is_used_unless_scan_is_requested(self):
        self.assertFalse(tracklist.build_parser().parse_args(["vid12345678"]).scan)
        self.assertTrue(tracklist.build_parser().parse_args(["vid12345678", "--scan"]).scan)


class ScanConflictTest(unittest.TestCase):
    def test_scanning_without_recognition_is_rejected_instead_of_ignored(self):
        parser = tracklist.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["vid12345678", "--scan", "--no-recognize"])


class ProbeDurationTest(unittest.TestCase):
    def run_probe(self, stdout, returncode=0):
        calls = []

        class Result:
            def __init__(self):
                self.stdout = stdout
                self.returncode = returncode

        original = tracklist.subprocess.run
        tracklist.subprocess.run = lambda command, **kwargs: calls.append(command) or Result()
        try:
            return tracklist.probe_duration("vid12345678"), calls[0]
        finally:
            tracklist.subprocess.run = original

    def test_reads_the_duration_yt_dlp_reports(self):
        duration, command = self.run_probe("3552\n")
        self.assertEqual(duration, 3552)
        self.assertIn("--print", command)
        self.assertIn("https://www.youtube.com/watch?v=vid12345678", command)

    def test_a_fractional_duration_is_truncated_to_whole_seconds(self):
        self.assertEqual(self.run_probe("3552.44\n")[0], 3552)

    def test_a_failed_probe_reports_no_duration(self):
        self.assertIsNone(self.run_probe("", returncode=1)[0])

    def test_unparseable_output_reports_no_duration(self):
        self.assertIsNone(self.run_probe("NA\n")[0])


class ScanPhaseLabelTest(unittest.TestCase):
    def test_the_probe_phase_explains_it_is_reading_the_video(self):
        self.assertEqual(recognize_tui.phase_label("probe"), "Leyendo duracion del video...")


class ScanGapsTest(unittest.TestCase):
    def test_a_scan_covers_the_whole_video_as_one_gap(self):
        self.assertEqual(tracklist.scan_gaps(3552), [(0, 3552)])

    def test_a_video_too_short_to_sample_yields_nothing(self):
        self.assertEqual(tracklist.scan_gaps(20), [])

    def test_an_unknown_duration_yields_nothing(self):
        self.assertEqual(tracklist.scan_gaps(None), [])


class SingleDownloadTest(IsolatedCache, unittest.TestCase):
    def test_downloads_once_and_cuts_with_absolute_offsets(self):
        if importlib.util.find_spec("shazamio") is None:
            self.skipTest("shazamio no disponible")
        import shazamio

        downloads = []
        cuts = []

        class FakeShazam:
            async def recognize_song(self, path):
                return {}

        original = (tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam)
        tracklist.download_audio = lambda video_id, directory: downloads.append(video_id) or Path("/tmp/a.mp3")
        tracklist.cut_sample = lambda source, offset, directory: cuts.append(offset) or Path("/tmp/s.wav")
        shazamio.Shazam = FakeShazam
        try:
            tracklist.recognize_gaps("vid12345678", [(0, 300), (600, 900)], lambda event: None)
        finally:
            tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam = original

        self.assertEqual(len(downloads), 1)
        self.assertEqual(sorted(cuts), [15, 75, 135, 195, 255, 615, 675, 735, 795, 855])


class ResumeTest(IsolatedCache, unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("shazamio") is None:
            self.skipTest("shazamio no disponible")
        super().setUp()

    def run_recognition(self, gaps, distinct=False):
        import shazamio

        self.downloads = []
        self.cuts = []
        events = []

        class FakeShazam:
            def __init__(self):
                self.calls = 0

            async def recognize_song(self, path):
                self.calls += 1
                if distinct:
                    return {"track": {"subtitle": f"A{self.calls}", "title": f"T{self.calls}"}}
                return {"track": {"subtitle": "A", "title": "T"}}

        def fake_download(video_id, directory):
            self.downloads.append(video_id)
            target = tracklist.audio_path(video_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"mp3")
            return target

        original = (tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam)
        tracklist.download_audio = fake_download
        tracklist.cut_sample = lambda source, offset, directory: self.cuts.append(offset) or Path("/tmp/s.wav")
        shazamio.Shazam = FakeShazam
        try:
            tracklist.recognize_gaps("vid12345678", gaps, events.append)
        finally:
            tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam = original
        return events

    def test_recognised_samples_are_cached_for_the_next_run(self):
        self.run_recognition([(0, 150)])
        match = {"artist": "A", "title": "T"}
        self.assertEqual(
            tracklist.load_cache("vid12345678")["samples"],
            {15: match, 75: match, 135: match},
        )

    def test_second_run_reuses_the_audio_instead_of_downloading_again(self):
        self.run_recognition([(0, 150)])
        self.run_recognition([(0, 150)])
        self.assertEqual(self.downloads, [])

    def test_second_run_skips_samples_it_already_recognised(self):
        self.run_recognition([(0, 150)])
        self.run_recognition([(0, 150)])
        self.assertEqual(self.cuts, [])

    def test_only_the_missing_samples_are_recognised_again(self):
        self.run_recognition([(0, 150)], distinct=True)
        self.run_recognition([(0, 270)], distinct=True)
        self.assertEqual(sorted(self.cuts), [195, 255])

    def test_every_sample_is_launched_without_waiting_for_a_batch(self):
        import shazamio

        peak = {"value": 0, "active": 0}

        idle = {"value": 0.0}

        class UnevenShazam:
            def __init__(self):
                self.calls = 0

            async def recognize_song(self, path):
                self.calls += 1
                peak["active"] += 1
                peak["value"] = max(peak["value"], peak["active"])
                await asyncio.sleep(0.30 if self.calls % 3 == 0 else 0.02)
                peak["active"] -= 1
                if peak["active"] == 0:
                    idle["value"] += 1
                return {"track": {"subtitle": f"A{self.calls}", "title": f"T{self.calls}"}}

        original = (tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam)
        tracklist.download_audio = lambda video_id, directory: Path("/tmp/a.mp3")
        tracklist.cut_sample = lambda source, offset, directory: Path("/tmp/s.wav")
        shazamio.Shazam = UnevenShazam
        try:
            tracklist.recognize_gaps("vid12345678", [(0, 1200)], lambda event: None)
        finally:
            tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam = original
        self.assertEqual(peak["value"], tracklist.MAX_WORKERS)
        self.assertLessEqual(idle["value"], 3)

    def test_cached_samples_still_reach_the_progress_callback(self):
        self.run_recognition([(0, 150)])
        events = self.run_recognition([(0, 150)])
        match = {"artist": "A", "title": "T"}
        samples = [event for event in events if event[0] == "sample"]
        self.assertEqual(
            samples,
            [("sample", 15, match), ("sample", 75, match), ("sample", 135, match)],
        )

    def test_a_fully_cached_run_still_yields_its_suggestions(self):
        self.run_recognition([(0, 150)])
        events = self.run_recognition([(0, 150)])
        self.assertEqual([event for event in events if event[0] == "done"][0][1], 1)

    def test_missing_audio_is_downloaded_again_without_losing_samples(self):
        self.run_recognition([(0, 150)])
        tracklist.audio_path("vid12345678").unlink()
        self.run_recognition([(0, 150)])
        self.assertEqual(self.downloads, ["vid12345678"])
        self.assertEqual(self.cuts, [])


if __name__ == "__main__":
    unittest.main()
