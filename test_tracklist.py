import asyncio
import contextlib
import hashlib
import io
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import messages
import recognize_tui
import recognize_ui
import tracklist
import upload


def setUpModule():
    messages.LANGUAGE = "en"


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


class StudioSessionTest(unittest.TestCase):
    COOKIES = {"SAPISID": "a", "__Secure-1PAPISID": "b", "__Secure-3PAPISID": "c"}

    def replace(self, name, value):
        original = getattr(tracklist, name)
        setattr(tracklist, name, value)
        self.addCleanup(setattr, tracklist, name, original)

    def test_a_session_needs_only_browser_cookies_and_a_client_version(self):
        session = tracklist.studio_session(self.COOKIES, 1700000000)
        self.assertEqual(
            set(session["headers"]),
            {"cookie", "authorization", "origin", "x-origin", "x-youtube-client-name", "x-youtube-client-version"},
        )
        self.assertEqual(session["headers"]["authorization"], tracklist.build_authorization(self.COOKIES, 1700000000))
        self.assertEqual(session["headers"]["origin"], tracklist.ORIGIN)
        self.assertEqual(session["headers"]["x-origin"], tracklist.ORIGIN)
        self.assertEqual(session["headers"]["x-youtube-client-name"], "62")
        self.assertEqual(session["headers"]["x-youtube-client-version"], tracklist.STUDIO_CLIENT_VERSION)
        self.assertEqual(
            session["context"],
            {"client": {"clientName": 62, "clientVersion": tracklist.STUDIO_CLIENT_VERSION}},
        )

    def test_a_client_version_without_a_realistic_shape_is_never_sent(self):
        self.assertRegex(tracklist.STUDIO_CLIENT_VERSION, r"1\.\d{8}\.\d{2}\.\d{2}")

    def test_a_saved_browser_that_lost_its_session_names_it_and_the_way_out(self):
        self.replace("current_browser", lambda: "firefox")
        self.replace("browser_cookies", lambda browser: {})
        with self.assertRaises(SystemExit) as stop:
            tracklist.fresh_session()
        self.assertIn("firefox", str(stop.exception.code))
        self.assertIn("--browser", str(stop.exception.code))

    def test_an_outdated_client_version_is_reported_as_such(self):
        def refuse(request):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b""))

        self.replace_urlopen(refuse)
        with self.assertRaises(SystemExit) as stop:
            tracklist.post(tracklist.CLAIMS_ENDPOINT, {}, tracklist.studio_session(self.COOKIES, 1))
        self.assertIn("STUDIO_CLIENT_VERSION", str(stop.exception.code))

    def test_a_rejected_session_tells_the_user_which_account_to_sign_in_with(self):
        def refuse(request):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        self.replace_urlopen(refuse)
        with self.assertRaises(SystemExit) as stop:
            tracklist.post(tracklist.CLAIMS_ENDPOINT, {}, tracklist.studio_session(self.COOKIES, 1))
        self.assertIn("YouTube rejected the session", str(stop.exception.code))

    def test_a_forbidden_video_says_the_browser_has_another_account_open(self):
        def refuse(request):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

        self.replace_urlopen(refuse)
        with self.assertRaises(SystemExit) as stop:
            tracklist.post(tracklist.CLAIMS_ENDPOINT, {}, tracklist.studio_session(self.COOKIES, 1))
        self.assertIn("The browser has another YouTube account open", str(stop.exception.code))
        self.assertIn("--browser", str(stop.exception.code))

    def test_a_spanish_system_gets_studio_errors_in_spanish(self):
        def refuse(request):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        self.replace_urlopen(refuse)
        with mock.patch.object(messages, "LANGUAGE", "es"), self.assertRaises(SystemExit) as stop:
            tracklist.post(tracklist.CLAIMS_ENDPOINT, {}, tracklist.studio_session(self.COOKIES, 1))
        self.assertIn("YouTube rechazo la sesion", str(stop.exception.code))

    def replace_urlopen(self, fake):
        original = urllib.request.urlopen
        urllib.request.urlopen = fake
        self.addCleanup(setattr, urllib.request, "urlopen", original)


class SafariAccessTest(unittest.TestCase):
    def test_safari_without_disk_access_says_where_to_grant_it(self):
        refused = subprocess.CompletedProcess([], 1, "", "Operation not permitted")
        with mock.patch.object(tracklist.subprocess, "run", return_value=refused), \
                self.assertRaises(SystemExit) as stop:
            tracklist.browser_cookies("safari")
        self.assertIn("System Settings > Privacy & Security > Full Disk Access", str(stop.exception.code))


class ReinstallHintTest(unittest.TestCase):
    def test_missing_dependencies_point_to_the_reinstall_command(self):
        shown = io.StringIO()
        with mock.patch.dict("sys.modules", {"shazamio": None}), contextlib.redirect_stderr(shown):
            self.assertFalse(tracklist.recognition_available())
        self.assertIn("Reinstall tracklist: uv tool install --force", shown.getvalue())


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
        self.assertIn("1 identified, 1 suggested, 0 gaps", text)

    def test_summary_omits_suggestions_when_none(self):
        entries = [{"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}]
        text = tracklist.render(entries, [], "video123456", False)
        self.assertIn("1 identified, 0 gaps", text)
        self.assertNotIn("suggested", text)

    def test_a_spanish_system_gets_the_tracklist_in_spanish(self):
        entries = [{"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}]
        with mock.patch.object(messages, "LANGUAGE", "es"):
            text = tracklist.render(entries, [(60, 200)], "video123456", False)
        self.assertIn("????? (sin identificar, hasta 03:20)", text)
        self.assertIn("1 identificados, 1 huecos (02:20 sin identificar de 03:20)", text)

    def test_unidentified_stretches_say_until_when_they_end(self):
        text = tracklist.render([], [(60, 200)], "video123456", False)
        self.assertIn("  - 01:00 ????? (unidentified, until 03:20)", text)
        self.assertIn("(02:20 unidentified of 02:20)", text)


class LanguageTest(unittest.TestCase):
    def test_a_spanish_locale_shows_spanish(self):
        self.assertEqual(messages.detect({"LANG": "es_ES.UTF-8"}), "es")

    def test_any_other_locale_falls_back_to_english_so_nobody_gets_a_language_we_lack(self):
        self.assertEqual(messages.detect({"LANG": "fr_FR.UTF-8"}), "en")
        self.assertEqual(messages.detect({"LANG": "C"}), "en")
        self.assertEqual(messages.detect({}), "en")

    def test_lc_all_overrides_lang_as_the_locale_convention_says(self):
        self.assertEqual(messages.detect({"LC_ALL": "es_ES.UTF-8", "LANG": "en_US.UTF-8"}), "es")
        self.assertEqual(messages.detect({"LC_MESSAGES": "en_US.UTF-8", "LANG": "es_ES.UTF-8"}), "en")

    def test_every_text_exists_in_both_languages_so_no_screen_breaks_halfway(self):
        self.assertEqual(set(messages.MESSAGES["en"]), set(messages.MESSAGES["es"]))


class PentestLineTest(unittest.TestCase):
    def test_formats_each_event_kind(self):
        self.assertEqual(recognize_ui.pentest_line(("gap", 1, 7, 0, 242)), "[*] gap 1/7 (00:00-04:02)")
        self.assertEqual(
            recognize_ui.pentest_line(("sample", 75, {"artist": "Duran Duran", "title": "Too Late Marlene"})),
            "[+] 01:15 Duran Duran - Too Late Marlene",
        )
        self.assertEqual(recognize_ui.pentest_line(("sample", 135, None)), "[-] 02:15 no match")
        self.assertEqual(
            recognize_ui.pentest_line(("backoff", 3, 1)),
            "[!] rate limit - waiting 3s (attempt 1/4)",
        )
        self.assertEqual(recognize_ui.pentest_line(("done", 8, 552)), "[*] 8 suggestions in 09:12")


class BackoffCountdownTest(unittest.TestCase):
    def test_the_log_says_how_long_it_waits_and_which_attempt(self):
        self.assertEqual(
            recognize_ui.pentest_line(("backoff", 12, 3)),
            "[!] rate limit - waiting 12s (attempt 3/4)",
        )

    def test_the_active_row_counts_the_wait_down(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [], "backoff": 8}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 searching (sample 2/4) - backoff 8s...[/cyan]")

    def test_a_row_not_waiting_shows_no_backoff(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [], "backoff": 0}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 searching (sample 2/4)...[/cyan]")

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
        self.assertEqual(recognize_ui.pentest_line(("workers", 1)), "[!] dropping to 1 concurrent request")

    def test_it_reports_when_concurrency_recovers(self):
        self.assertEqual(recognize_ui.pentest_line(("workers", 3)), "[*] raising to 3 concurrent requests")


class VerboseLogTest(unittest.TestCase):
    def test_it_says_how_many_claims_studio_returned(self):
        self.assertEqual(recognize_ui.pentest_line(("claims", 18)), "[*] 18 Content ID claims")

    def test_a_single_claim_is_not_pluralised(self):
        self.assertEqual(recognize_ui.pentest_line(("claims", 1)), "[*] 1 Content ID claim")

    def test_it_says_when_the_video_is_not_claimed(self):
        self.assertEqual(
            recognize_ui.pentest_line(("claims", 0)),
            "[*] no claims: scanning the whole video with Shazam",
        )

    def test_it_announces_the_download_and_its_end(self):
        self.assertEqual(recognize_ui.pentest_line(("download", "start")), "[*] downloading audio...")
        self.assertEqual(recognize_ui.pentest_line(("download", "done")), "[*] audio downloaded")

    def test_it_says_when_the_audio_was_already_on_disk(self):
        self.assertEqual(recognize_ui.pentest_line(("download", "cached")), "[*] audio already downloaded")

    def test_it_reports_how_much_is_reused_on_resume(self):
        self.assertEqual(
            recognize_ui.pentest_line(("cached", 19, 54)),
            "[*] 19/54 samples already analysed, 35 to go",
        )

    def test_a_recovered_backoff_is_reported_so_the_warning_is_not_left_hanging(self):
        self.assertEqual(recognize_ui.pentest_line(("resolved", 795)), "[*] 13:15 recovered after the backoff")

    def test_a_spanish_system_gets_the_log_in_spanish(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(recognize_ui.pentest_line(("claims", 2)), "[*] 2 reclamaciones de Content ID")
            self.assertEqual(recognize_ui.pentest_line(("sample", 135, None)), "[-] 02:15 sin match")

    def test_an_unknown_event_still_prints_nothing(self):
        self.assertIsNone(recognize_ui.pentest_line(("cualquier-cosa",)))


class OfferClipboardTest(unittest.TestCase):
    def answer(self, reply):
        with mock.patch.object(recognize_ui.sys, "stdin") as stdin, \
                mock.patch.object(recognize_ui.sys, "stderr", io.StringIO()) as stderr, \
                mock.patch("builtins.input", return_value=reply), \
                mock.patch.object(recognize_ui.subprocess, "run") as run:
            stdin.isatty.return_value = True
            stderr.isatty = lambda: True
            recognize_ui.offer_clipboard("tracklist")
        return run.called, stderr.getvalue()

    def test_english_asks_with_y_because_that_is_the_yes_an_english_speaker_types(self):
        copied, shown = self.answer("y")
        self.assertTrue(copied)
        self.assertIn("Copy to clipboard? [Y/n]", shown)

    def test_spanish_keeps_s_for_si(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            copied, shown = self.answer("s")
        self.assertTrue(copied)
        self.assertIn("¿Copiar al portapapeles? [S/n]", shown)

    def test_another_letter_does_not_copy(self):
        copied, _ = self.answer("n")
        self.assertFalse(copied)


ENTRY = {"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}
SUGGESTION = {"artist": "B", "title": "U", "label": "", "start": 75, "duration": 10, "suggested": True}
GAPS = [(60, 300), (400, 600)]


class TuiRowsTest(unittest.TestCase):
    def test_initial_state_shows_entries_and_pending_gaps(self):
        self.assertEqual(
            recognize_ui.tui_rows([ENTRY], GAPS, {}, None),
            [
                "[green]  - 00:00 A - T[/green]",
                "[dim]  - 01:00 ????? (unidentified, until 05:00)[/dim]",
                "[dim]  - 06:40 ????? (unidentified, until 10:00)[/dim]",
            ],
        )

    def test_active_gap_shows_progress_and_partial_matches(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [SUGGESTION], "backoff": False}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 searching (sample 2/4)...[/cyan]")
        self.assertEqual(rows[2], "[yellow]  - 01:15 B - U (?)[/yellow]")
        self.assertEqual(rows[3], "[dim]  - 06:40 ????? (unidentified, until 10:00)[/dim]")

    def test_active_gap_before_first_sample_shows_download(self):
        active = {"index": 0, "sample": 0, "total": 4, "matches": [], "backoff": False}
        self.assertEqual(
            recognize_ui.tui_rows([ENTRY], GAPS, {}, active)[1],
            "[cyan]  - 01:00 downloading...[/cyan]",
        )

    def test_completed_gap_consolidates_suggestions_and_residuals(self):
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {0: [SUGGESTION]}, None)
        self.assertEqual(rows[1], "[yellow]  - 01:15 B - U (?)[/yellow]")
        self.assertEqual(rows[2], "[dim]  - 01:25 ????? (unidentified, until 05:00)[/dim]")

    def test_completed_late_gap_adds_no_spurious_leading_residual(self):
        late = {"artist": "C", "title": "V", "label": "", "start": 415, "duration": 10, "suggested": True}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {1: [late]}, None)
        self.assertNotIn("[dim]  - 00:00 ????? (unidentified, until 06:40)[/dim]", rows)
        self.assertIn("[yellow]  - 06:55 C - V (?)[/yellow]", rows)
        self.assertIn("[dim]  - 07:05 ????? (unidentified, until 10:00)[/dim]", rows)

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
            "Done - 8 suggestions",
        )

    def test_finished_run_with_no_suggestions_says_so(self):
        state = {**STATE, "phase": "done", "paused": False, "suggestions": 0}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Done - no suggestions",
        )

    def test_a_single_suggestion_is_not_pluralised(self):
        state = {**STATE, "phase": "done", "paused": False, "suggestions": 1}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Done - 1 suggestion",
        )

    def test_pause_takes_precedence_over_everything(self):
        state = {**STATE, "phase": "done", "paused": True}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "PAUSED",
        )

    def test_running_state_still_reports_the_current_sample(self):
        state = {**STATE, "phase": "recognizing", "paused": False}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Sample 7/12",
        )

    def test_loading_phases_keep_their_label(self):
        state = {**STATE, "phase": "claims", "paused": False}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "Asking Studio for claims...",
        )

    def test_backoff_is_reported_while_running(self):
        state = {**STATE, "phase": "recognizing", "paused": False, "backoff_until": 105.0}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
            "rate limit - backoff 3s",
        )


class SpanishStatusTest(unittest.TestCase):
    def test_a_spanish_system_gets_the_status_in_spanish(self):
        state = {**STATE, "phase": "done", "paused": False}
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(
                recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0)),
                "Completado - 8 sugerencias",
            )
            self.assertEqual(recognize_tui.phase_label("claims"), "Pidiendo reclamaciones a Studio...")


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
        self.assertEqual(recognize_tui.phase_label("cookies"), "Reading browser cookies...")
        self.assertEqual(recognize_tui.phase_label("claims"), "Asking Studio for claims...")
        self.assertEqual(recognize_tui.phase_label("video"), "Fetching video details...")
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

    def test_help_follows_the_system_language(self):
        self.assertIn("URL or ID of the video, or an audio file", tracklist.build_parser().format_help())
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertIn("URL o ID del video, o fichero de audio", tracklist.build_parser().format_help())

    def test_the_mode_is_detected_instead_of_being_asked_for(self):
        parser = tracklist.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["vid12345678", "--scan"])



class TryClaimsTest(unittest.TestCase):
    def call(self, session_error=None, claims_error=None, response=None, own=False):
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
            return tracklist.try_claims("vid12345678", own)
        finally:
            tracklist.fresh_session, tracklist.fetch_claims = original

    def test_a_claimed_video_returns_its_entries(self):
        response = {"receivedClaims": [{
            "matchDetails": {"longestMatchStartTimeSeconds": 60, "longestMatchDurationSeconds": 30},
            "asset": {"metadata": {"soundRecording": {"title": "T", "artists": ["A"]}}},
        }]}
        session, entries, failure = self.call(response=response)
        self.assertIsNotNone(session)
        self.assertEqual(entries[0]["title"], "T")
        self.assertIsNone(failure)

    def test_a_video_without_claims_reports_no_entries(self):
        session, entries, failure = self.call(response={"receivedClaims": []})
        self.assertEqual(entries, [])

    def test_a_missing_session_falls_back_instead_of_exiting(self):
        session, entries, failure = self.call(session_error=SystemExit("sin sesion"))
        self.assertIsNone(session)
        self.assertEqual(entries, [])

    def test_a_foreign_video_refused_by_studio_falls_back_quietly_since_403_is_expected_there(self):
        session, entries, failure = self.call(claims_error=SystemExit("403"))
        self.assertIsNone(session)
        self.assertEqual(entries, [])
        self.assertIsNone(failure)

    def test_studio_refusing_my_own_video_is_reported_because_only_the_user_can_fix_it(self):
        session, entries, failure = self.call(claims_error=SystemExit("403"), own=True)
        self.assertIsNone(session)
        self.assertEqual(failure, "403")

    def test_an_outdated_client_version_is_reported_even_for_foreign_videos_since_it_breaks_every_video(self):
        stale = tracklist.t("studio_404", version=tracklist.STUDIO_CLIENT_VERSION)
        session, entries, failure = self.call(claims_error=SystemExit(stale))
        self.assertEqual(failure, stale)


class StudioFailureMainTest(unittest.TestCase):
    COOKIES = {"SAPISID": "a", "__Secure-1PAPISID": "b", "__Secure-3PAPISID": "c"}

    def run_main(self, own, code, *flags):
        def refuse(request, *args, **kwargs):
            raise urllib.error.HTTPError(request.full_url, code, "No", {}, io.BytesIO(b""))

        errors = io.StringIO()
        with mock.patch.object(tracklist, "missing_tools", lambda: []), \
                mock.patch.object(upload, "adopt", lambda video_id, report: (video_id, None, None, own)), \
                mock.patch.object(upload, "offer_delete", lambda video_id: None), \
                mock.patch.object(tracklist, "fresh_session", lambda: tracklist.studio_session(self.COOKIES, 1)), \
                mock.patch.object(tracklist, "load_cache", lambda video_id: {"samples": {}, "title": None, "duration": None}), \
                mock.patch.object(urllib.request, "urlopen", refuse), \
                mock.patch("sys.argv", ["tracklist", "orig1234567", "--duration", "600", *flags]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            tracklist.main()
        return errors.getvalue()

    def test_my_own_video_refused_by_studio_tells_me_to_switch_account(self):
        self.assertIn("The browser has another YouTube account open", self.run_main(True, 403, "--no-recognize"))

    def test_a_spanish_system_gets_the_switch_account_hint_in_spanish(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            errors = self.run_main(True, 403, "--no-recognize")
        self.assertIn("El navegador tiene abierta otra cuenta de YouTube", errors)

    def test_a_foreign_video_refused_by_studio_falls_back_to_shazam_without_alarming_the_user(self):
        self.assertNotIn("another YouTube account", self.run_main(False, 403, "--no-recognize"))

    def test_an_outdated_client_version_is_shown_even_for_a_foreign_video(self):
        self.assertIn("STUDIO_CLIENT_VERSION", self.run_main(False, 404, "--no-recognize"))

    def test_the_tui_is_told_the_video_is_mine(self):
        seen = []

        def fake_app(video_id, duration, label, own):
            seen.append(own)
            return [], [], [], 600, None, None

        with mock.patch.object(tracklist, "recognition_available", lambda: True), \
                mock.patch.object(recognize_ui, "textual_available", lambda: True), \
                mock.patch.object(recognize_tui, "run_app", fake_app):
            self.run_main(True, 403)
        self.assertEqual(seen, [True])


class AutoModeTest(unittest.TestCase):
    def plan(self, entries, duration):
        return tracklist.find_gaps(entries, duration) if entries else tracklist.scan_gaps(duration)

    def test_a_claimed_video_only_samples_its_gaps(self):
        entries = [{"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}]
        self.assertEqual(self.plan(entries, 600), [(60, 600)])

    def test_an_unclaimed_video_samples_everything(self):
        self.assertEqual(self.plan([], 600), [(0, 600)])


class ProbeDurationTest(unittest.TestCase):
    def setUp(self):
        original = tracklist.current_browser
        tracklist.current_browser = lambda: "chrome"
        self.addCleanup(setattr, tracklist, "current_browser", original)

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

    def test_yt_dlp_reads_cookies_from_the_saved_browser(self):
        tracklist.current_browser = lambda: "firefox"
        _, command = self.run_probe("3552\n")
        index = command.index("--cookies-from-browser")
        self.assertEqual(command[index:index + 2], ["--cookies-from-browser", "firefox"])


class BrowserTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        originals = (tracklist.CONFIG_FILE, tracklist.APP_DIRS, tracklist.browser_cookies)
        tracklist.CONFIG_FILE = self.root / "config" / "config.json"
        tracklist.APP_DIRS = [self.root / "apps"]

        def restore():
            tracklist.CONFIG_FILE, tracklist.APP_DIRS, tracklist.browser_cookies = originals

        self.addCleanup(restore)

    def install(self, *apps):
        for app in apps:
            (self.root / "apps" / app).mkdir(parents=True)

    def test_only_installed_browsers_are_tried_so_macos_does_not_ask_for_every_keychain(self):
        self.install("Firefox.app", "Google Chrome.app")
        self.assertEqual(tracklist.installed_browsers([self.root / "apps"]), ["chrome", "firefox"])

    def test_the_first_browser_signed_in_to_youtube_wins(self):
        read = lambda browser: {"SAPISID": "x"} if browser == "firefox" else {}
        self.assertEqual(tracklist.pick_browser(["chrome", "firefox"], read), "firefox")

    def test_a_browser_whose_cookies_cannot_be_read_is_skipped(self):
        def read(browser):
            if browser == "chrome":
                raise SystemExit("locked")
            return {"SAPISID": "x"}

        self.assertEqual(tracklist.pick_browser(["chrome", "firefox"], read), "firefox")

    def test_the_detected_browser_is_remembered_instead_of_probed_every_run(self):
        self.install("Firefox.app")
        reads = []
        tracklist.browser_cookies = lambda browser: reads.append(browser) or {"SAPISID": "x"}
        self.assertEqual(tracklist.current_browser(), "firefox")
        self.assertEqual(tracklist.current_browser(), "firefox")
        self.assertEqual(reads, ["firefox"])
        self.assertEqual(json.loads(tracklist.CONFIG_FILE.read_text()), {"browser": "firefox"})

    def test_no_browser_with_a_youtube_session_explains_what_to_do(self):
        with self.assertRaises(SystemExit) as raised:
            tracklist.current_browser()
        self.assertIn("studio.youtube.com", str(raised.exception.code))

    def test_browser_flag_replaces_the_saved_choice(self):
        tracklist.CONFIG_FILE.parent.mkdir(parents=True)
        tracklist.CONFIG_FILE.write_text(json.dumps({"browser": "brave", "otra": 1}))
        tracklist.save_browser("chrome")
        self.assertEqual(json.loads(tracklist.CONFIG_FILE.read_text()), {"browser": "chrome", "otra": 1})


class ScanPhaseLabelTest(unittest.TestCase):
    def test_the_probe_phase_explains_it_is_reading_the_video(self):
        self.assertEqual(recognize_tui.phase_label("probe"), "Reading video length...")


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


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeOwnershipClient:
    def __init__(self, my_channel, video_items):
        self.my_channel = my_channel
        self.video_items = video_items

    def channels(self):
        return self

    def videos(self):
        return self

    def list(self, **kwargs):
        if "mine" in kwargs:
            return FakeRequest({"items": [{"id": self.my_channel}]})
        return FakeRequest({"items": self.video_items})


def video_item(channel_id, title):
    return {"snippet": {"channelId": channel_id, "title": title}}


class VideoOwnerTest(unittest.TestCase):
    def test_a_video_of_my_channel_is_mine(self):
        client = FakeOwnershipClient("UCme", [video_item("UCme", "Mi set")])
        self.assertEqual(upload.video_owner(client, "vid12345678"), (True, "Mi set"))

    def test_a_video_of_another_channel_is_not_mine_and_keeps_its_title(self):
        client = FakeOwnershipClient("UCme", [video_item("UCother", "Set ajeno")])
        self.assertEqual(upload.video_owner(client, "vid12345678"), (False, "Set ajeno"))

    def test_a_video_youtube_does_not_show_is_treated_as_foreign(self):
        client = FakeOwnershipClient("UCme", [])
        self.assertEqual(upload.video_owner(client, "vid12345678"), (False, None))


class NoChannelClient(FakeOwnershipClient):
    def list(self, **kwargs):
        if "mine" in kwargs:
            return FakeRequest({"items": []})
        return super().list(**kwargs)


class NoChannelTest(unittest.TestCase):
    def test_an_account_without_a_youtube_channel_asks_for_one_instead_of_crashing(self):
        client = NoChannelClient("UCme", [])
        with self.assertRaises(SystemExit) as raised:
            upload.video_owner(client, "vid12345678")
        self.assertIn("has no YouTube channel", str(raised.exception))


class DownloadForeignTest(IsolatedCache, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.commands = []
        self.returncode = 0
        original = (upload.subprocess.run, tracklist.current_browser)

        def fake_run(command, **kwargs):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, self.returncode)

        def restore():
            upload.subprocess.run, tracklist.current_browser = original

        self.addCleanup(restore)
        upload.subprocess.run = fake_run
        tracklist.current_browser = lambda: "firefox"

    def test_the_download_uses_the_users_yt_dlp_options_and_only_audio(self):
        upload.download_foreign("vid12345678")
        command = self.commands[0]
        size = len(upload.DOWNLOAD_OPTIONS)
        windows = [command[i:i + size] for i in range(len(command) - size + 1)]
        self.assertIn(upload.DOWNLOAD_OPTIONS, windows)

    def test_an_audio_already_downloaded_is_reused(self):
        upload.downloads_dir().mkdir(parents=True)
        done = upload.downloads_dir() / "vid12345678.webm"
        done.write_bytes(b"audio")
        self.assertEqual(upload.download_foreign("vid12345678"), done)
        self.assertEqual(self.commands, [])

    def test_a_half_finished_download_is_not_mistaken_for_a_finished_one(self):
        upload.downloads_dir().mkdir(parents=True)
        (upload.downloads_dir() / "vid12345678.webm.part").write_bytes(b"medio")
        upload.download_foreign("vid12345678")
        self.assertEqual(len(self.commands), 1)

    def test_yt_dlp_leftovers_of_an_interrupted_download_are_not_reused(self):
        upload.downloads_dir().mkdir(parents=True)
        for name in ("vid12345678.webm.part", "vid12345678.webm.ytdl",
                     "vid12345678.webm.temp", "vid12345678.f251.webm.part-Frag3"):
            (upload.downloads_dir() / name).write_bytes(b"medio")
        self.assertIsNone(upload.downloaded_audio("vid12345678"))

    def test_a_failed_download_reports_nothing_to_upload(self):
        self.returncode = 1
        self.assertIsNone(upload.download_foreign("vid12345678"))


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
        tracklist.try_claims = lambda video_id, own: (
            ({} if session else None),
            [{"start": 0}] * next(polls),
            None if session else "Studio said no",
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
        self.assertIn("Studio said no", str(stop.exception.code))
        self.assertIn("https://youtu.be/vid12345678", str(stop.exception.code))

    def test_giving_up_tells_the_user_to_retry_later(self):
        polls = upload.CLAIMS_TIMEOUT // upload.CLAIMS_POLL + 1
        time, messages = self.wait([0] * polls)
        self.assertEqual(time.now, upload.CLAIMS_TIMEOUT)
        self.assertIn("again later", messages[-1])


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
        self.upload_error = upload.QuotaExceeded()
        with self.assertRaises(upload.QuotaExceeded):
            upload.prepare(self.source, lambda message: None)
        self.assertIsNone(upload.known_upload(self.source))

    def test_a_given_title_wins_over_the_file_name(self):
        upload.prepare(self.source, lambda message: None, "Set original")
        self.assertEqual(self.uploads, ["Set original"])


class AdoptTest(IsolatedUploads, IsolatedCache, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.uploaded = []
        self.downloads = 0
        self.owner = (False, "Set ajeno")
        self.client_error = None
        self.quota_error = False
        self.messages = []
        names = ("youtube_client", "video_owner", "download_foreign", "convert_to_video",
                 "upload_video", "wait_for_processing", "wait_for_claims")
        original = {name: getattr(upload, name) for name in names}
        self.real_download_foreign = upload.download_foreign
        original_run = upload.subprocess.run

        def client():
            if self.client_error:
                raise self.client_error
            return "client"

        def fake_upload(client, path, title):
            if self.quota_error:
                raise upload.QuotaExceeded()
            self.uploaded.append(title)
            return "copia123456"

        def fake_download(video_id):
            self.downloads += 1
            upload.downloads_dir().mkdir(parents=True, exist_ok=True)
            path = upload.downloads_dir() / f"{video_id}.webm"
            path.write_bytes(b"audio")
            return path

        upload.youtube_client = client
        upload.video_owner = lambda client, video_id: self.owner
        upload.download_foreign = fake_download
        upload.convert_to_video = lambda source, target: target.write_bytes(b"mp4")
        upload.upload_video = fake_upload
        upload.wait_for_processing = lambda client, video_id, report: None
        upload.wait_for_claims = lambda video_id, report: None

        def restore():
            for name, value in original.items():
                setattr(upload, name, value)
            upload.subprocess.run = original_run

        self.addCleanup(restore)

    def adopt(self):
        return upload.adopt("orig1234567", self.messages.append)

    def test_my_own_video_is_read_directly_without_uploading(self):
        self.owner = (True, "Mi set")
        self.assertEqual(self.adopt(), ("orig1234567", None, None, True))
        self.assertEqual((self.uploaded, self.downloads), ([], 0))

    def test_a_foreign_video_is_uploaded_with_its_original_title(self):
        video_id, audio, title, own = self.adopt()
        self.assertEqual((video_id, title, own), ("copia123456", "Set ajeno", True))
        self.assertEqual(audio, upload.downloads_dir() / "orig1234567.webm")
        self.assertEqual(self.uploaded, ["Set ajeno"])

    def test_relaunching_a_foreign_url_neither_downloads_nor_uploads_again(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            target = command[command.index("-o") + 1].replace("%(ext)s", "webm")
            Path(target).write_bytes(b"audio")
            return subprocess.CompletedProcess(command, 0)

        upload.download_foreign = self.real_download_foreign
        upload.subprocess.run = fake_run
        original_browser = tracklist.current_browser
        tracklist.current_browser = lambda: "firefox"
        self.addCleanup(setattr, tracklist, "current_browser", original_browser)
        self.adopt()
        self.adopt()
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(len(commands), 1)

    def test_without_a_google_login_it_keeps_working_with_shazam_alone(self):
        self.client_error = SystemExit("Falta la credencial")
        self.assertEqual(self.adopt(), ("orig1234567", None, None, False))
        self.assertTrue(any("Shazam only" in message for message in self.messages))

    def test_an_account_without_a_channel_keeps_working_with_shazam_alone(self):
        def no_channel(client, video_id):
            raise SystemExit("Tu cuenta de Google no tiene canal de YouTube")

        upload.video_owner = no_channel
        self.assertEqual(self.adopt(), ("orig1234567", None, None, False))
        self.assertTrue(any("Shazam only" in message for message in self.messages))

    def test_an_api_error_while_checking_ownership_falls_back_to_shazam(self):
        from googleapiclient.errors import HttpError

        def failing(client, video_id):
            raise HttpError(mock.Mock(status=403, reason="quota"), b"quotaExceeded")

        upload.video_owner = failing
        self.assertEqual(self.adopt(), ("orig1234567", None, None, False))
        self.assertTrue(any("Shazam only" in message for message in self.messages))

    def test_an_interrupted_wait_already_remembers_which_copy_belongs_to_the_original(self):
        def interrupted(video_id, report):
            raise SystemExit("Studio no deja leer")

        upload.wait_for_claims = interrupted
        with self.assertRaises(SystemExit):
            self.adopt()
        self.assertEqual(upload.load_uploads()["youtube:orig1234567"], "copia123456")

    def test_a_video_that_cannot_be_downloaded_falls_back_to_shazam(self):
        upload.download_foreign = lambda video_id: None
        self.assertEqual(self.adopt(), ("orig1234567", None, "Set ajeno", False))
        self.assertIn("Could not download the video: continuing with Shazam only", self.messages)

    def test_an_exhausted_quota_falls_back_to_shazam_instead_of_stopping(self):
        self.quota_error = True
        self.assertEqual(self.adopt(), ("orig1234567", None, "Set ajeno", False))
        self.assertIn("YouTube upload quota used up: continuing with Shazam only", self.messages)

    def test_deleting_the_uploaded_copy_also_deletes_the_download(self):
        video_id, audio, title, own = self.adopt()
        upload.forget_upload(video_id)
        self.assertFalse(audio.exists())
        self.assertFalse(any(key.startswith("youtube:") for key in upload.load_uploads()))

    def test_the_tracklist_points_to_the_original_video_not_the_copy(self):
        names = ("adopt", "offer_delete")
        original = {name: getattr(upload, name) for name in names}
        original_tracklist = {name: getattr(tracklist, name) for name in ("try_claims", "fetch_videos", "missing_tools")}
        upload.adopt = lambda video_id, report: ("copia123456", None, "Set", True)
        upload.offer_delete = lambda video_id: None
        tracklist.missing_tools = lambda: []
        tracklist.try_claims = lambda video_id, own: (None, [{"start": 0, "duration": 60, "title": "Tema", "artist": "A"}], None)
        tracklist.fetch_videos = lambda ids, session: {}

        def restore():
            for name, value in original.items():
                setattr(upload, name, value)
            for name, value in original_tracklist.items():
                setattr(tracklist, name, value)

        self.addCleanup(restore)
        output = io.StringIO()
        with mock.patch("sys.argv", ["tracklist", "orig1234567", "--no-recognize", "--duration", "600"]):
            with contextlib.redirect_stdout(output):
                tracklist.main()
        self.assertIn("watch?v=orig1234567", output.getvalue())
        self.assertIn("Set", output.getvalue())
        self.assertNotIn("copia123456", output.getvalue())


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

        with mock.patch.object(tracklist, "try_claims", lambda video_id, own: ({}, next(looks), None)), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(App, "run", drive):
            _, entries, gaps, _, _, _ = recognize_tui.run_app("dnS4s0gar-4", 600, False)
        self.assertEqual([entry["title"] for entry in entries], ["Kisses of Fire"])
        self.assertEqual(gaps, [(300, 600)])


class ConfirmKeyTest(unittest.TestCase):
    def bindings(self):
        from textual.app import App

        seen = []
        with mock.patch.object(App, "run", lambda app, *args, **kwargs: seen.append(app)):
            recognize_tui.run_app("dnS4s0gar-4", 600, False)
        return {action: (key, description) for key, action, description in seen[0].BINDINGS}

    def test_english_confirms_with_y_to_match_the_yes_it_asks_for(self):
        self.assertEqual(self.bindings()["confirm"], ("y", "Confirm"))
        self.assertEqual(self.bindings()["pause"], ("p", "Pause"))

    def test_spanish_confirms_with_s_for_si(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(self.bindings()["confirm"], ("s", "Confirmar"))
            self.assertEqual(self.bindings()["pause"], ("p", "Pausa"))


class TypedUrlHintTest(unittest.TestCase):
    HINT = "[!] if the video is not yours, quit and run 'tracklist <url>' to upload it and read Content ID"

    def log_after_loading(self, typed_id):
        from textual.app import App

        async def no_recognition(video_id, gaps, progress, resume=None):
            return []

        logged = []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test() as pilot:
                    await app.workers.wait_for_complete()
                    if typed_id:
                        await pilot.press("n")
                        app.query_one("#video-input").value = typed_id
                        await pilot.press("enter")
                    else:
                        await pilot.press("r")
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    logged.extend(line.text for line in app.query_one("#log").lines)
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(tracklist, "try_claims", lambda video_id, own: (None, [], None)), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(App, "run", drive):
            recognize_tui.run_app("dnS4s0gar-4", 600, False)
        return logged

    def test_a_foreign_url_typed_in_the_tui_explains_how_to_get_content_id(self):
        self.assertIn(self.HINT, [line.strip() for line in self.log_after_loading("dnS4s0gar-5")])

    def test_the_hint_is_not_shown_for_the_video_tracklist_was_launched_with(self):
        self.assertNotIn(self.HINT, [line.strip() for line in self.log_after_loading(None)])


class TuiStudioFailureTest(unittest.TestCase):
    MESSAGE = "The browser has another YouTube account open"

    def log_after_loading(self, own):
        from textual.app import App

        async def no_recognition(video_id, gaps, progress, resume=None):
            return []

        def refuse(video_id, session):
            raise SystemExit(f"{self.MESSAGE}: switch")

        logged = []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test() as pilot:
                    await app.workers.wait_for_complete()
                    logged.extend(line.text for line in app.query_one("#log").lines)
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(tracklist, "fresh_session", lambda: {"headers": {}, "context": {}}), \
                mock.patch.object(tracklist, "fetch_claims", refuse), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(App, "run", drive):
            recognize_tui.run_app("dnS4s0gar-4", 600, False, own)
        return "\n".join(logged)

    def test_studio_refusing_my_own_video_is_shown_in_the_log(self):
        self.assertIn(f"[!] {self.MESSAGE}", self.log_after_loading(True))

    def test_a_foreign_video_keeps_the_quiet_fallback(self):
        self.assertNotIn(self.MESSAGE, self.log_after_loading(False))


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
        asked = self.offer("otro1234567", answer="y")
        asked.assert_not_called()
        self.assertEqual(self.deleted, [])

    def test_just_pressing_enter_keeps_the_video_because_deleting_is_permanent(self):
        self.offer("dnS4s0gar-4", answer="")
        self.assertEqual(self.deleted, [])
        self.assertEqual(upload.known_upload(self.source), "dnS4s0gar-4")

    def test_confirming_deletes_the_video_and_forgets_the_upload(self):
        self.offer("dnS4s0gar-4", answer="y")
        self.assertEqual(self.deleted, ["dnS4s0gar-4"])
        self.assertIsNone(upload.known_upload(self.source))

    def test_spanish_confirms_with_s_for_si(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.offer("dnS4s0gar-4", answer="s")
        self.assertEqual(self.deleted, ["dnS4s0gar-4"])

    def test_s_does_not_delete_in_english_because_it_is_not_yes_there(self):
        self.offer("dnS4s0gar-4", answer="s")
        self.assertEqual(self.deleted, [])

    def test_without_a_terminal_it_never_asks(self):
        asked = self.offer("dnS4s0gar-4", answer="y", tty=False)
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


class SetupCredentialsTest(unittest.TestCase):
    DESKTOP = {"installed": {"client_id": "abc.apps.googleusercontent.com"}}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        original = upload.CLIENT_SECRET
        upload.CLIENT_SECRET = self.root / "config" / "client_secret.json"

        def restore():
            upload.CLIENT_SECRET = original

        self.addCleanup(restore)
        self.opened = []
        self.reported = []
        self.now = 1000.0

    def run_wizard(self, answers, on_ask=None):
        replies = iter(answers)

        def ask(prompt):
            if on_ask:
                on_ask()
            return next(replies)

        upload.setup_credentials(
            open_url=self.opened.append, ask=ask, report=self.reported.append,
            downloads=self.downloads, clock=lambda: self.now,
        )

    def write_secret(self, path, content=None, mtime=None):
        path.write_text(json.dumps(content or self.DESKTOP))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_a_desktop_client_downloaded_during_the_wizard_is_installed(self):
        def download_after_start():
            self.write_secret(self.downloads / "client_secret_1.json", mtime=self.now + 10)

        self.run_wizard([""] * 7, on_ask=download_after_start)
        self.assertTrue(upload.CLIENT_SECRET.exists())
        self.assertEqual(upload.CLIENT_SECRET.stat().st_mode & 0o777, 0o600)

    def test_an_old_client_secret_in_downloads_is_ignored(self):
        self.write_secret(self.downloads / "client_secret_old.json", mtime=self.now - 100)
        fresh = self.write_secret(self.root / "fresh.json")
        self.run_wizard([""] * 7 + [str(fresh)])
        self.assertTrue(upload.CLIENT_SECRET.exists())
        self.assertEqual(len([m for m in self.reported if m.startswith("That file")]), 1)

    def test_a_json_that_is_not_a_desktop_client_is_rejected(self):
        web = self.write_secret(self.root / "web.json", {"web": {"client_id": "x"}})
        good = self.write_secret(self.root / "good.json")
        self.run_wizard([""] * 6 + [str(web), str(good)])
        self.assertTrue(any("Desktop app" in m for m in self.reported))
        self.assertEqual(json.loads(upload.CLIENT_SECRET.read_text()), self.DESKTOP)

    def test_a_dragged_path_with_quotes_is_accepted(self):
        good = self.write_secret(self.root / "client_secret.json")
        self.run_wizard([""] * 6 + [f"'{good}' "])
        self.assertTrue(upload.CLIENT_SECRET.exists())

    def test_every_step_opens_its_console_page(self):
        good = self.write_secret(self.root / "client_secret.json")
        self.run_wizard([""] * 6 + [str(good)])
        self.assertEqual(self.opened, upload.CONSOLE_URLS)
        self.assertEqual(len(self.opened), 5)

    def test_spanish_steps_also_name_the_english_button_since_the_console_follows_the_google_account(self):
        good = self.write_secret(self.root / "client_secret.json")
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.run_wizard([""] * 6 + [str(good)])
        self.assertTrue(any("'App de escritorio' (Desktop app)" in m for m in self.reported))
        self.assertTrue(any(m.startswith("Paso 1/5") for m in self.reported))


class MissingToolsTest(unittest.TestCase):
    def test_missing_system_tools_are_named_so_the_user_knows_what_to_install(self):
        found = {"yt-dlp": "/usr/bin/yt-dlp"}
        self.assertEqual(tracklist.missing_tools(which=found.get), ["ffmpeg", "ffprobe"])


class PackagingTest(unittest.TestCase):
    def test_the_installed_command_runs_main(self):
        import tomllib

        root = Path(tracklist.__file__).parent
        config = tomllib.loads((root / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["scripts"]["tracklist"], "tracklist:main")
        for module in config["tool"]["setuptools"]["py-modules"]:
            self.assertTrue((root / f"{module}.py").is_file(), module)

    def test_every_module_is_installed_so_imports_work_after_uv_tool_install(self):
        import tomllib

        root = Path(tracklist.__file__).parent
        config = tomllib.loads((root / "pyproject.toml").read_text())
        modules = {path.stem for path in root.glob("*.py") if not path.stem.startswith("test_")}
        self.assertEqual(set(config["tool"]["setuptools"]["py-modules"]), modules)


if __name__ == "__main__":
    unittest.main()
