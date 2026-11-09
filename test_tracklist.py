import asyncio
import contextlib
import hashlib
import io
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recognize_tui
import recognize_ui
import tracklist
import upload


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
        self.assertEqual(tracklist.plan_samples(0, 120), [15, 105])
        self.assertEqual(tracklist.plan_samples(120, 300), [135, 225])

    def test_sample_that_does_not_fit_is_dropped(self):
        self.assertEqual(tracklist.plan_samples(0, 114), [15])
        self.assertEqual(tracklist.plan_samples(0, 24), [])

    def test_the_interval_stays_under_the_shortest_gap_it_must_cover(self):
        self.assertLessEqual(tracklist.SAMPLE_INTERVAL, tracklist.MIN_GAP_SECONDS)


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
        self.assertEqual(
            recognize_ui.pentest_line(("backoff", 3, 1)),
            "[!] rate limit - esperando 3s (intento 1/4)",
        )
        self.assertEqual(recognize_ui.pentest_line(("done", 8, 552)), "[*] 8 sugerencias en 09:12")


class BackoffCountdownTest(unittest.TestCase):
    def test_the_log_says_how_long_it_waits_and_which_attempt(self):
        self.assertEqual(
            recognize_ui.pentest_line(("backoff", 12, 3)),
            "[!] rate limit - esperando 12s (intento 3/4)",
        )

    def test_the_active_row_counts_the_wait_down(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [], "backoff": 8}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 buscando (muestra 2/4) - backoff 8s...[/cyan]")

    def test_a_row_not_waiting_shows_no_backoff(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [], "backoff": 0}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 buscando (muestra 2/4)...[/cyan]")

    def test_the_status_panel_counts_the_wait_down(self):
        state = {**STATE, "phase": "recognizing", "paused": False, "backoff_until": 112.0}
        fields = recognize_tui.status_fields(state, 102.0)
        self.assertEqual(recognize_tui.status_text(state, fields), "rate limit - backoff 10s")

    def test_the_countdown_never_goes_negative(self):
        state = {**STATE, "phase": "recognizing", "paused": False, "backoff_until": 100.0}
        fields = recognize_tui.status_fields(state, 130.0)
        self.assertFalse(fields["backoff"])


class LimiterLogTest(unittest.TestCase):
    def test_it_reports_when_concurrency_drops(self):
        self.assertEqual(recognize_ui.pentest_line(("workers", 1)), "[!] bajando a 1 peticion simultanea")

    def test_it_reports_when_concurrency_recovers(self):
        self.assertEqual(recognize_ui.pentest_line(("workers", 3)), "[*] subiendo a 3 peticiones simultaneas")


class VerboseLogTest(unittest.TestCase):
    def test_it_says_how_many_claims_studio_returned(self):
        self.assertEqual(recognize_ui.pentest_line(("claims", 18)), "[*] 18 reclamaciones de Content ID")

    def test_a_single_claim_is_not_pluralised(self):
        self.assertEqual(recognize_ui.pentest_line(("claims", 1)), "[*] 1 reclamacion de Content ID")

    def test_it_says_when_the_video_is_not_claimed(self):
        self.assertEqual(
            recognize_ui.pentest_line(("claims", 0)),
            "[*] sin reclamaciones: se analiza el video entero con Shazam",
        )

    def test_it_announces_the_download_and_its_end(self):
        self.assertEqual(recognize_ui.pentest_line(("download", "start")), "[*] descargando audio...")
        self.assertEqual(recognize_ui.pentest_line(("download", "done")), "[*] audio descargado")

    def test_it_says_when_the_audio_was_already_on_disk(self):
        self.assertEqual(recognize_ui.pentest_line(("download", "cached")), "[*] audio ya descargado")

    def test_it_reports_how_much_is_reused_on_resume(self):
        self.assertEqual(
            recognize_ui.pentest_line(("cached", 19, 54)),
            "[*] 19/54 muestras ya analizadas, faltan 35",
        )

    def test_a_recovered_backoff_is_reported_so_the_warning_is_not_left_hanging(self):
        self.assertEqual(recognize_ui.pentest_line(("resolved", 795)), "[*] 13:15 recuperado tras el backoff")

    def test_an_unknown_event_still_prints_nothing(self):
        self.assertIsNone(recognize_ui.pentest_line(("cualquier-cosa",)))


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
        fields = recognize_tui.status_fields({**STATE, "backoff_until": 175.0}, 160.0)
        self.assertEqual(fields["percent"], 50)
        self.assertEqual(fields["backoff"], 15)

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
        state = {**STATE, "phase": "recognizing", "paused": False, "backoff_until": 105.0}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "rate limit - backoff 3s",
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
        with mock.patch.dict(os.environ, {}, clear=True):
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

    def test_the_mode_is_detected_instead_of_being_asked_for(self):
        parser = tracklist.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["vid12345678", "--scan"])



class TryClaimsTest(unittest.TestCase):
    def call(self, session_error=None, claims_error=None, response=None):
        original = (tracklist.fresh_session, tracklist.fetch_claims)

        def fake_session():
            if session_error:
                raise session_error
            return {"headers": {}, "context": {}}

        def fake_claims(video_id, session):
            if claims_error:
                raise claims_error
            return response

        tracklist.fresh_session = fake_session
        tracklist.fetch_claims = fake_claims
        try:
            return tracklist.try_claims("vid12345678")
        finally:
            tracklist.fresh_session, tracklist.fetch_claims = original

    def test_a_claimed_video_returns_its_entries(self):
        response = {"receivedClaims": [{
            "matchDetails": {"longestMatchStartTimeSeconds": 60, "longestMatchDurationSeconds": 30},
            "asset": {"metadata": {"soundRecording": {"title": "T", "artists": ["A"]}}},
        }]}
        session, entries = self.call(response=response)
        self.assertIsNotNone(session)
        self.assertEqual(entries[0]["title"], "T")

    def test_a_video_without_claims_reports_no_entries(self):
        session, entries = self.call(response={"receivedClaims": []})
        self.assertEqual(entries, [])

    def test_a_missing_session_falls_back_instead_of_exiting(self):
        session, entries = self.call(session_error=SystemExit("sin sesion"))
        self.assertIsNone(session)
        self.assertEqual(entries, [])

    def test_a_rejected_session_falls_back_instead_of_exiting(self):
        session, entries = self.call(claims_error=SystemExit("403"))
        self.assertIsNone(session)
        self.assertEqual(entries, [])


class AutoModeTest(unittest.TestCase):
    def plan(self, entries, duration):
        return tracklist.find_gaps(entries, duration) if entries else tracklist.scan_gaps(duration)

    def test_a_claimed_video_only_samples_its_gaps(self):
        entries = [{"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}]
        self.assertEqual(self.plan(entries, 600), [(60, 600)])

    def test_an_unclaimed_video_samples_everything(self):
        self.assertEqual(self.plan([], 600), [(0, 600)])


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

    def test_reads_cookies_from_the_browser_the_user_chose(self):
        with mock.patch.dict(os.environ, {"TRACKLIST_BROWSER": "firefox"}):
            command = self.run_probe("3552\n")[1]
        self.assertEqual(command[command.index("--cookies-from-browser") + 1], "firefox")

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
        self.assertEqual(
            sorted(cuts),
            tracklist.plan_samples(0, 300) + tracklist.plan_samples(600, 900),
        )


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
            {offset: match for offset in tracklist.plan_samples(0, 150)},
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
        already = set(tracklist.plan_samples(0, 150))
        self.assertEqual(
            sorted(self.cuts),
            [o for o in tracklist.plan_samples(0, 270) if o not in already],
        )

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

    def test_samples_shazam_failed_on_are_retried_next_run_instead_of_cached_as_unknown(self):
        import shazamio
        from shazamio.exceptions import FailedDecodeJson

        class RefusingShazam:
            async def recognize_song(self, path):
                raise FailedDecodeJson("rate limited")

        original = (tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam)
        tracklist.download_audio = lambda video_id, directory: Path("/tmp/a.mp3")
        tracklist.cut_sample = lambda source, offset, directory: Path("/tmp/s.wav")
        shazamio.Shazam = RefusingShazam
        try:
            with mock.patch.object(tracklist, "backoff_delay", lambda failures: 0):
                tracklist.recognize_gaps("vid12345678", [(0, 150)], lambda event: None)
        finally:
            tracklist.download_audio, tracklist.cut_sample, shazamio.Shazam = original
        self.assertEqual(tracklist.load_cache("vid12345678")["samples"], {})

    def test_cached_samples_still_reach_the_progress_callback(self):
        self.run_recognition([(0, 150)])
        events = self.run_recognition([(0, 150)])
        match = {"artist": "A", "title": "T"}
        samples = [event for event in events if event[0] == "sample"]
        self.assertEqual(
            samples,
            [("sample", offset, match) for offset in tracklist.plan_samples(0, 150)],
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


def ffprobe_streams(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return sorted(result.stdout.split())


class LocalAudio(IsolatedCache):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "Spook Multiespacio 3.m4a"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:a", "aac", str(self.source)],
            check=True, capture_output=True,
        )


class ConvertToVideoTest(LocalAudio, unittest.TestCase):
    def test_youtube_gets_a_video_whose_audio_is_untouched(self):
        target = self.root / "out.mp4"
        upload.convert_to_video(self.source, target)
        self.assertEqual(ffprobe_streams(target), ["aac,audio", "h264,video"])

    def test_the_picture_is_the_recording_waveform_instead_of_a_black_frame(self):
        target = self.root / "out.mp4"
        upload.convert_to_video(self.source, target)
        frame = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(target), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, check=True,
        ).stdout
        self.assertGreater(max(frame), 128)


class LocalDurationTest(LocalAudio, unittest.TestCase):
    def test_the_length_comes_from_the_file_since_a_private_video_may_be_unreadable(self):
        self.assertEqual(upload.local_duration(self.source), 2)


class PlaceAudioTest(LocalAudio, unittest.TestCase):
    def test_shazam_finds_the_local_audio_where_a_download_would_be(self):
        upload.place_audio(self.source, "vid12345678")
        self.assertEqual(ffprobe_streams(tracklist.audio_path("vid12345678")), ["mp3,audio"])

    def test_existing_audio_is_reused_instead_of_converted_again(self):
        tracklist.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        tracklist.audio_path("vid12345678").write_bytes(b"ya estaba")
        upload.place_audio(self.source, "vid12345678")
        self.assertEqual(tracklist.audio_path("vid12345678").read_bytes(), b"ya estaba")

    def test_audio_cleared_by_fresh_is_rebuilt_from_the_local_file(self):
        upload.place_audio(self.source, "vid12345678")
        tracklist.clear_cache("vid12345678")
        upload.place_audio(self.source, "vid12345678")
        self.assertTrue(tracklist.audio_path("vid12345678").exists())


class ClaimsSettledTest(unittest.TestCase):
    def test_the_same_count_three_times_means_content_id_is_done(self):
        self.assertTrue(upload.claims_settled([2, 5, 5, 5], 480))

    def test_a_count_still_growing_keeps_waiting(self):
        self.assertFalse(upload.claims_settled([5, 5, 6], 360))

    def test_no_claims_yet_is_not_mistaken_for_done(self):
        self.assertFalse(upload.claims_settled([0, 0, 0], 360))

    def test_waiting_gives_up_after_an_hour(self):
        self.assertTrue(upload.claims_settled([0, 0, 0], upload.CLAIMS_TIMEOUT))


class FakeTime:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


class WaitForClaimsTest(unittest.TestCase):
    def wait(self, counts, session=True):
        polls = iter(counts)
        original = tracklist.try_claims
        tracklist.try_claims = lambda video_id: (
            ({} if session else None),
            [{"start": 0}] * next(polls),
        )
        time = FakeTime()
        messages = []
        try:
            upload.wait_for_claims("vid12345678", messages.append, time.sleep, time.clock)
        finally:
            tracklist.try_claims = original
        return time, messages

    def test_stops_polling_once_the_claims_stop_changing(self):
        time, _ = self.wait([0, 3, 7, 7, 7, 9])
        self.assertEqual(time.sleeps, [upload.CLAIMS_POLL] * 4)

    def test_without_a_studio_session_it_stops_so_claims_are_not_skipped(self):
        with self.assertRaises(SystemExit) as stop:
            self.wait([0], session=False)
        self.assertIn("studio.youtube.com", str(stop.exception.code))

    def test_giving_up_tells_the_user_to_retry_later(self):
        polls = upload.CLAIMS_TIMEOUT // upload.CLAIMS_POLL + 1
        time, messages = self.wait([0] * polls)
        self.assertEqual(time.now, upload.CLAIMS_TIMEOUT)
        self.assertIn("mas tarde", messages[-1])


class WaitForProcessingTest(unittest.TestCase):
    def wait(self, statuses):
        polls = iter(statuses)
        original = upload.processing_status
        upload.processing_status = lambda client, video_id: next(polls)
        time = FakeTime()
        try:
            upload.wait_for_processing("client", "vid12345678", lambda message: None, time.sleep)
        finally:
            upload.processing_status = original
        return time

    def test_claims_are_only_read_after_youtube_finishes_processing(self):
        time = self.wait(["processing", "processing", "succeeded"])
        self.assertEqual(time.sleeps, [upload.PROCESSING_POLL] * 2)

    def test_a_failed_upload_stops_instead_of_waiting_forever(self):
        with self.assertRaises(SystemExit):
            self.wait(["processing", "failed"])

    def test_a_terminated_upload_stops_instead_of_waiting_forever(self):
        with self.assertRaises(SystemExit):
            self.wait(["processing", "terminated"])


class IsolatedUploads:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        original = upload.UPLOADS_FILE
        upload.UPLOADS_FILE = self.root / "config" / "uploads.json"

        def restore():
            upload.UPLOADS_FILE = original

        self.addCleanup(restore)


class UploadRegistryTest(IsolatedUploads, unittest.TestCase):
    def test_a_file_never_uploaded_is_unknown(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        self.assertIsNone(upload.known_upload(source))

    def test_a_renamed_file_is_still_recognised_by_its_content(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        upload.remember_upload(source, "vid12345678")
        renamed = self.root / "otro nombre.m4a"
        source.rename(renamed)
        self.assertEqual(upload.known_upload(renamed), "vid12345678")

    def test_a_different_recording_with_the_same_name_is_not_confused(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        upload.remember_upload(source, "vid12345678")
        source.write_bytes(b"otra grabacion")
        self.assertIsNone(upload.known_upload(source))


class PrepareTest(IsolatedUploads, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "Spook Multiespacio 3.m4a"
        self.source.write_bytes(b"audio")
        self.uploads = []
        self.waits = []
        self.upload_error = None
        names = ("youtube_client", "convert_to_video", "upload_video",
                 "wait_for_processing", "wait_for_claims")
        original = {name: getattr(upload, name) for name in names}

        def fake_upload(client, path, title):
            if self.upload_error:
                raise self.upload_error
            self.uploads.append(title)
            return "vid12345678"

        upload.youtube_client = lambda: "client"
        upload.convert_to_video = lambda source, target: target.write_bytes(b"mp4")
        upload.upload_video = fake_upload
        upload.wait_for_processing = lambda client, video_id, report: self.waits.append(("processing", video_id))
        upload.wait_for_claims = lambda video_id, report: self.waits.append(("claims", video_id))

        def restore():
            for name, value in original.items():
                setattr(upload, name, value)

        self.addCleanup(restore)

    def test_the_video_is_titled_after_the_file(self):
        self.assertEqual(upload.prepare(self.source, lambda message: None), "vid12345678")
        self.assertEqual(self.uploads, ["Spook Multiespacio 3"])

    def test_relaunching_after_an_interrupted_wait_does_not_upload_twice(self):
        upload.prepare(self.source, lambda message: None)
        upload.prepare(self.source, lambda message: None)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.waits[-2:], [("processing", "vid12345678"), ("claims", "vid12345678")])

    def test_a_failed_upload_is_retried_on_the_next_run(self):
        self.upload_error = SystemExit("cuota")
        with self.assertRaises(SystemExit):
            upload.prepare(self.source, lambda message: None)
        self.assertIsNone(upload.known_upload(self.source))


class LocalFileTest(unittest.TestCase):
    def test_an_existing_recording_takes_the_upload_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Spook Multiespacio 3.m4a"
            source.write_bytes(b"audio")
            self.assertEqual(tracklist.local_file(str(source)), source)

    def test_a_video_id_keeps_the_youtube_path(self):
        self.assertIsNone(tracklist.local_file("vid12345678"))

    def test_a_video_url_keeps_the_youtube_path(self):
        self.assertIsNone(tracklist.local_file("https://www.youtube.com/watch?v=vid12345678"))


class ExpiredTokenTest(unittest.TestCase):
    def test_a_revoked_token_asks_to_log_in_again_instead_of_crashing(self):
        from google.auth.exceptions import RefreshError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret, token = root / "client_secret.json", root / "youtube_token.json"
            secret.write_text("{}")
            token.write_text("{}")
            stale = mock.Mock(expired=True, refresh_token="r")
            stale.refresh.side_effect = RefreshError("invalid_grant")
            fresh = mock.Mock(valid=True)
            fresh.to_json.return_value = "{}"
            flow = mock.Mock()
            flow.run_local_server.return_value = fresh
            with mock.patch.object(upload, "CLIENT_SECRET", secret), \
                    mock.patch.object(upload, "TOKEN_FILE", token), \
                    mock.patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=stale), \
                    mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", return_value=flow), \
                    mock.patch("googleapiclient.discovery.build", return_value="client"):
                self.assertEqual(upload.youtube_client(), "client")
            flow.run_local_server.assert_called_once()


class RefreshKeyTest(unittest.TestCase):
    def test_r_picks_up_claims_content_id_added_after_the_first_look(self):
        from textual.app import App

        claim = {"receivedClaims": [{
            "matchDetails": {"longestMatchStartTimeSeconds": 0, "longestMatchDurationSeconds": 300},
            "asset": {"metadata": {"soundRecording": {"title": "Kisses of Fire", "artists": ["La Luna"]}}},
        }]}
        looks = iter([[], tracklist.extract(claim)])

        async def no_recognition(video_id, gaps, progress, resume=None):
            return []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test() as pilot:
                    await app.workers.wait_for_complete()
                    await pilot.press("r")
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(tracklist, "try_claims", lambda video_id: ({}, next(looks))), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(App, "run", drive):
            _, entries, gaps, _, _, _ = recognize_tui.run_app("dnS4s0gar-4", 600, False)
        self.assertEqual([entry["title"] for entry in entries], ["Kisses of Fire"])
        self.assertEqual(gaps, [(300, 600)])


class CopiedTitleTest(IsolatedCache, unittest.TestCase):
    def test_a_known_length_does_not_lose_the_cached_title_that_heads_the_tracklist(self):
        from textual.app import App

        tracklist.save_video_details("dnS4s0gar-4", "Spook Multiespacio 3", 600)

        async def no_recognition(video_id, gaps, progress, resume=None):
            return []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test() as pilot:
                    await app.workers.wait_for_complete()
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(tracklist, "try_claims", lambda video_id: ({}, [])), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(App, "run", drive):
            _, _, _, _, title, _ = recognize_tui.run_app("dnS4s0gar-4", 600, False)
        self.assertEqual(title, "Spook Multiespacio 3")


class OfferDeleteTest(IsolatedUploads, unittest.TestCase):
    def setUp(self):
        super().setUp()
        source = self.root / "Spook Multiespacio 8.m4a"
        source.write_bytes(b"audio")
        self.source = source
        upload.remember_upload(source, "dnS4s0gar-4")
        self.deleted = []
        patches = [
            mock.patch.object(upload, "youtube_client", lambda: "client"),
            mock.patch.object(upload, "delete_video", lambda client, video_id: self.deleted.append(video_id)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def offer(self, video_id, answer="", tty=True):
        terminal = mock.Mock(isatty=lambda: tty)
        with mock.patch("sys.stdin", terminal), mock.patch("sys.stderr", terminal), \
                mock.patch("builtins.input", return_value=answer) as asked:
            upload.offer_delete(video_id)
        return asked

    def test_a_video_tracklist_did_not_upload_is_never_offered_for_deletion(self):
        asked = self.offer("otro1234567", answer="s")
        asked.assert_not_called()
        self.assertEqual(self.deleted, [])

    def test_just_pressing_enter_keeps_the_video_because_deleting_is_permanent(self):
        self.offer("dnS4s0gar-4", answer="")
        self.assertEqual(self.deleted, [])
        self.assertEqual(upload.known_upload(self.source), "dnS4s0gar-4")

    def test_confirming_deletes_the_video_and_forgets_the_upload(self):
        self.offer("dnS4s0gar-4", answer="s")
        self.assertEqual(self.deleted, ["dnS4s0gar-4"])
        self.assertIsNone(upload.known_upload(self.source))

    def test_without_a_terminal_it_never_asks(self):
        asked = self.offer("dnS4s0gar-4", answer="s", tty=False)
        asked.assert_not_called()
        self.assertEqual(self.deleted, [])


class DeleteScopeTest(unittest.TestCase):
    def test_a_token_granted_before_deletion_existed_asks_to_log_in_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret, token = root / "client_secret.json", root / "youtube_token.json"
            secret.write_text("{}")
            token.write_text("{}")
            narrow = mock.Mock(valid=True, expired=False)
            narrow.has_scopes.return_value = False
            fresh = mock.Mock(valid=True)
            fresh.to_json.return_value = "{}"
            flow = mock.Mock()
            flow.run_local_server.return_value = fresh
            with mock.patch.object(upload, "CLIENT_SECRET", secret), \
                    mock.patch.object(upload, "TOKEN_FILE", token), \
                    mock.patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=narrow), \
                    mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", return_value=flow), \
                    mock.patch("googleapiclient.discovery.build", return_value="client"):
                upload.youtube_client()
            flow.run_local_server.assert_called_once()


class MergeAcrossRecognitionGapTest(unittest.TestCase):
    def test_one_track_survives_a_sample_that_recognised_nothing(self):
        match = {"artist": "Kay Cee", "title": "Millenium Stringz"}
        merged = tracklist.merge_samples([(15, match), (105, None), (195, match)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["start"], 15)
        self.assertEqual(merged[0]["duration"], 190)

    def test_a_track_played_twice_in_a_set_stays_two_entries(self):
        encore = {"artist": "Kay Cee", "title": "Millenium Stringz"}
        other = {"artist": "Transa", "title": "Supernova"}
        merged = tracklist.merge_samples([(15, encore), (105, other), (195, encore)])
        self.assertEqual(len(merged), 3)

    def test_a_distant_repeat_is_not_folded_into_one_entry(self):
        match = {"artist": "Kay Cee", "title": "Millenium Stringz"}
        merged = tracklist.merge_samples([(15, match), (600, match)])
        self.assertEqual(len(merged), 2)


class FoldSuggestionsIntoClaimsTest(unittest.TestCase):
    def claim(self, start, duration, artist, title):
        return {"artist": artist, "title": title, "label": "",
                "start": start, "duration": duration}

    def suggest(self, start, artist, title):
        return {"artist": artist, "title": title, "label": "",
                "start": start, "duration": 10, "suggested": True}

    def test_a_restated_track_just_after_a_claim_is_absorbed(self):
        claimed = [self.claim(172, 95, "Kay Cee", "Millenium Stringz (Radio Edit)")]
        suggestion = self.suggest(277, "Kay Cee", "Millenium Stringz (Radio Edit)")
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [])

    def test_a_claim_grows_to_cover_the_suggestion_it_absorbs(self):
        claimed = [self.claim(172, 95, "Kay Cee", "Millenium Stringz (Radio Edit)")]
        tracklist.drop_claimed(claimed, [self.suggest(277, "Kay Cee", "Millenium Stringz (Radio Edit)")])
        self.assertEqual(claimed[0]["start"], 172)
        self.assertEqual(claimed[0]["duration"], 115)

    def test_a_suggestion_just_before_a_claim_hands_over_its_earlier_start(self):
        claimed = [self.claim(468, 190, "Signum", "Coming On Strong (Extended Mix)")]
        suggestion = self.suggest(457, "Signum", "Coming On Strong")
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [])
        self.assertEqual(claimed[0]["start"], 457)
        self.assertEqual(claimed[0]["title"], "Coming On Strong (Extended Mix)")

    def test_a_different_track_near_a_claim_is_left_alone(self):
        claimed = [self.claim(172, 95, "Kay Cee", "Millenium Stringz")]
        suggestion = self.suggest(277, "Transa", "Supernova")
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [suggestion])

    def test_the_same_track_far_from_a_claim_is_a_genuine_replay(self):
        claimed = [self.claim(172, 95, "Kay Cee", "Millenium Stringz")]
        suggestion = self.suggest(900, "Kay Cee", "Millenium Stringz")
        self.assertEqual(tracklist.drop_claimed(claimed, [suggestion]), [suggestion])


class AudioCacheDirTest(unittest.TestCase):
    home = Path("/home/ana")

    def test_macos_keeps_the_library_caches_folder(self):
        path = tracklist.audio_cache_dir("darwin", {}, self.home)
        self.assertEqual(path, self.home / "Library" / "Caches" / "tracklist")

    def test_windows_uses_localappdata_so_big_audio_stays_out_of_roaming_profiles(self):
        path = tracklist.audio_cache_dir("win32", {"LOCALAPPDATA": "C:/Users/ana/AppData/Local"}, self.home)
        self.assertEqual(path, Path("C:/Users/ana/AppData/Local") / "tracklist" / "Cache")

    def test_windows_without_localappdata_falls_back_to_the_default_location(self):
        path = tracklist.audio_cache_dir("win32", {}, self.home)
        self.assertEqual(path, self.home / "AppData" / "Local" / "tracklist" / "Cache")

    def test_linux_honours_xdg_cache_home(self):
        path = tracklist.audio_cache_dir("linux", {"XDG_CACHE_HOME": "/data/cache"}, self.home)
        self.assertEqual(path, Path("/data/cache") / "tracklist")

    def test_linux_defaults_to_dot_cache(self):
        path = tracklist.audio_cache_dir("linux", {}, self.home)
        self.assertEqual(path, self.home / ".cache" / "tracklist")


class BrowserNameTest(unittest.TestCase):
    def test_defaults_to_brave_so_existing_setups_keep_working(self):
        self.assertEqual(tracklist.browser_name({}), "brave")

    def test_the_environment_picks_the_browser_whose_cookies_hold_the_youtube_session(self):
        self.assertEqual(tracklist.browser_name({"TRACKLIST_BROWSER": "chrome"}), "chrome")

    def test_an_empty_value_falls_back_to_the_default(self):
        self.assertEqual(tracklist.browser_name({"TRACKLIST_BROWSER": ""}), "brave")

    def test_the_cookies_phase_names_the_chosen_browser(self):
        with mock.patch.dict(os.environ, {"TRACKLIST_BROWSER": "firefox"}):
            self.assertEqual(recognize_tui.phase_label("cookies"), "Leyendo cookies de Firefox...")


class BrowserCookiesTest(unittest.TestCase):
    def run_cookies(self, run):
        with mock.patch.dict(os.environ, {"TRACKLIST_BROWSER": "edge"}), \
                mock.patch.object(tracklist.subprocess, "run", side_effect=run):
            return tracklist.browser_cookies()

    def test_a_missing_yt_dlp_points_to_installing_it_without_assuming_homebrew(self):
        def run(command, **kwargs):
            raise FileNotFoundError

        with self.assertRaises(SystemExit) as raised:
            self.run_cookies(run)
        self.assertNotIn("brew", str(raised.exception))
        self.assertIn("yt-dlp", str(raised.exception))

    def test_a_cookie_read_failure_names_the_chosen_browser_and_how_to_change_it(self):
        def run(command, **kwargs):
            self.assertEqual(command[command.index("--cookies-from-browser") + 1], "edge")
            return mock.Mock(stderr="boom")

        with self.assertRaises(SystemExit) as raised:
            self.run_cookies(run)
        message = str(raised.exception)
        self.assertIn("edge", message)
        self.assertIn("TRACKLIST_BROWSER", message)


class MissingDependencyMessageTest(unittest.TestCase):
    def assert_portable(self, text):
        self.assertNotIn(".venv", text)
        self.assertNotIn("brew", text)
        self.assertIn("pip install", text)

    def test_missing_shazam_tells_how_to_install_it_on_any_platform(self):
        with mock.patch.dict("sys.modules", {"shazamio": None}), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(tracklist.recognition_available())
        self.assert_portable(stderr.getvalue())

    def test_recognizing_without_shazam_exits_with_a_portable_hint(self):
        with mock.patch.dict("sys.modules", {"shazamio": None}):
            with self.assertRaises(SystemExit) as raised:
                tracklist.recognize_gaps_async("vid12345678", [], lambda event: None)
        self.assert_portable(str(raised.exception))

    def test_missing_textual_tells_how_to_install_it_on_any_platform(self):
        stderr = io.StringIO()
        stderr.isatty = lambda: True
        with mock.patch.dict("sys.modules", {"textual": None}), \
                contextlib.redirect_stderr(stderr):
            self.assertFalse(recognize_ui.textual_available())
        self.assert_portable(stderr.getvalue())

    def test_missing_google_libraries_tell_how_to_install_them_on_any_platform(self):
        with mock.patch.dict("sys.modules", {"google.auth.exceptions": None}):
            with self.assertRaises(SystemExit) as raised:
                upload.youtube_client()
        self.assert_portable(str(raised.exception))


class ClipboardCommandTest(unittest.TestCase):
    def available(self, *names):
        return lambda name: f"/bin/{name}" if name in names else None

    def test_macos_copies_with_pbcopy(self):
        self.assertEqual(tracklist.clipboard_command("darwin", self.available()), ["pbcopy"])

    def test_windows_copies_with_clip_so_the_tracklist_reaches_the_clipboard_off_macos(self):
        self.assertEqual(tracklist.clipboard_command("win32", self.available()), ["clip"])

    def test_linux_prefers_wayland_when_both_tools_exist(self):
        command = tracklist.clipboard_command("linux", self.available("wl-copy", "xclip"))
        self.assertEqual(command, ["wl-copy"])

    def test_linux_falls_back_to_xclip_selecting_the_clipboard(self):
        command = tracklist.clipboard_command("linux", self.available("xclip"))
        self.assertEqual(command, ["xclip", "-selection", "clipboard"])

    def test_linux_without_any_tool_has_no_command(self):
        self.assertIsNone(tracklist.clipboard_command("linux", self.available()))


class CopyToClipboardTest(unittest.TestCase):
    def test_pipes_the_text_to_the_platform_command(self):
        with mock.patch.object(tracklist, "clipboard_command", return_value=["clip"]), \
                mock.patch.object(tracklist.subprocess, "run") as run:
            self.assertTrue(tracklist.copy_to_clipboard("hola"))
        run.assert_called_once_with(["clip"], input=b"hola", check=True)

    def test_reports_failure_when_no_tool_is_installed(self):
        with mock.patch.object(tracklist, "clipboard_command", return_value=None):
            self.assertFalse(tracklist.copy_to_clipboard("hola"))

    def test_reports_failure_when_the_tool_fails(self):
        error = subprocess.CalledProcessError(1, "clip")
        with mock.patch.object(tracklist, "clipboard_command", return_value=["clip"]), \
                mock.patch.object(tracklist.subprocess, "run", side_effect=error):
            self.assertFalse(tracklist.copy_to_clipboard("hola"))


if __name__ == "__main__":
    unittest.main()
