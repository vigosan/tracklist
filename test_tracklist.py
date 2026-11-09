import asyncio
import contextlib
import hashlib
import io
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import messages
import recognize_tui
import recognize_ui
import tracklist


def setUpModule():
    patch = mock.patch.object(tracklist, "update_notice", lambda: None)
    patch.start()
    unittest.addModuleCleanup(patch.stop)
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


class Utf8SubprocessTextTest(unittest.TestCase):
    def test_cookie_errors_are_decoded_as_utf8_so_windows_codepages_cannot_crash_them(self):
        refused = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(tracklist.subprocess, "run", return_value=refused) as run, \
                self.assertRaises(SystemExit):
            tracklist.browser_cookies("firefox")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_tracked_commands_are_decoded_as_utf8_so_a_title_outside_the_codepage_cannot_crash_them(self):
        process = mock.MagicMock()
        process.stdout = io.StringIO("")
        process.returncode = 0
        process.poll.return_value = 0
        with mock.patch.object(upload.subprocess, "Popen", return_value=process) as popen:
            upload.run_tracked(["yt-dlp"], lambda line: None, None, None)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")


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
        with mock.patch.object(tracklist, "clipboard_command", return_value=["pbcopy"]), \
                mock.patch.object(tracklist.subprocess, "run") as run:
            self.assertTrue(tracklist.copy_to_clipboard("Señal"))
        run.assert_called_once_with(["pbcopy"], input="Señal".encode("utf-8"), check=True)

    def test_clip_gets_utf16_with_a_byte_order_mark_so_accents_survive_its_codepage(self):
        with mock.patch.object(tracklist, "clipboard_command", return_value=["clip"]), \
                mock.patch.object(tracklist.subprocess, "run") as run:
            tracklist.copy_to_clipboard("Señal")
        self.assertEqual(run.call_args.kwargs["input"], "Señal".encode("utf-16"))

    def test_reports_failure_when_no_tool_is_installed(self):
        with mock.patch.object(tracklist, "clipboard_command", return_value=None):
            self.assertFalse(tracklist.copy_to_clipboard("hola"))

    def test_reports_failure_when_the_tool_fails(self):
        error = subprocess.CalledProcessError(1, "clip")
        with mock.patch.object(tracklist, "clipboard_command", return_value=["clip"]), \
                mock.patch.object(tracklist.subprocess, "run", side_effect=error):
            self.assertFalse(tracklist.copy_to_clipboard("hola"))


class OfferClipboardTest(unittest.TestCase):
    def answer(self, reply):
        with mock.patch.object(recognize_ui.sys, "stdin") as stdin, \
                mock.patch.object(recognize_ui.sys, "stderr", io.StringIO()) as stderr, \
                mock.patch("builtins.input", return_value=reply), \
                mock.patch.object(tracklist.subprocess, "run") as run:
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
        self.assertEqual(
            cache,
            {"samples": {}, "title": None, "duration": None, "content_id_done": False},
        )

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

    def test_saving_a_sample_keeps_the_content_id_mark(self):
        tracklist.mark_content_id_done("vid12345678")
        tracklist.save_sample("vid12345678", 15, None)
        tracklist.save_video_details("vid12345678", "Un set", 3600)
        self.assertTrue(tracklist.load_cache("vid12345678")["content_id_done"])

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
        self.assertIn("URL or ID of the video", tracklist.build_parser().format_help())
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertIn("URL o ID del video", tracklist.build_parser().format_help())

    def test_help_tells_how_to_update_in_either_language(self):
        self.assertIn("'update' to update tracklist", " ".join(tracklist.build_parser().format_help().split()))
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertIn("'update' para actualizar tracklist", " ".join(tracklist.build_parser().format_help().split()))

    def test_help_says_it_takes_my_videos_other_peoples_and_local_files(self):
        description = " ".join(tracklist.build_parser().format_help().split())
        self.assertIn("your own channel", description)
        self.assertIn("someone else's video", description)
        self.assertIn("a local audio file", description)
        with mock.patch.object(messages, "LANGUAGE", "es"):
            description = " ".join(tracklist.build_parser().format_help().split())
        self.assertIn("tu canal", description)
        self.assertIn("un video ajeno", description)
        self.assertIn("un fichero de audio", description)

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


class StudioFailureMainTest(IsolatedUploads, unittest.TestCase):
    COOKIES = {"SAPISID": "a", "__Secure-1PAPISID": "b", "__Secure-3PAPISID": "c"}

    def run_main(self, own, code, *flags):
        def refuse(request, *args, **kwargs):
            raise urllib.error.HTTPError(request.full_url, code, "No", {}, io.BytesIO(b""))

        errors = io.StringIO()
        with mock.patch.object(tracklist, "missing_tools", lambda: []), \
                fake_job("orig1234567", None, own), \
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

        def fake_app(video_id, duration, label, own, waiting=False, job=None, shown_id=None):
            seen.append(own)
            return tui_result()

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


class BrowsersWithProfileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self, *relative):
        for path in relative:
            (self.root / path).mkdir(parents=True)

    def test_windows_finds_chromium_browsers_under_local_app_data_and_firefox_under_roaming(self):
        self.make(
            "Local/Google/Chrome/User Data",
            "Local/Microsoft/Edge/User Data",
            "Roaming/Mozilla/Firefox/Profiles",
        )
        env = {"LOCALAPPDATA": str(self.root / "Local"), "APPDATA": str(self.root / "Roaming")}
        found = tracklist.browsers_with_profile("win32", env, self.root / "home")
        self.assertEqual(found, ["chrome", "edge", "firefox"])

    def test_windows_without_the_environment_falls_back_to_the_default_profile_locations(self):
        self.make("AppData/Local/BraveSoftware/Brave-Browser/User Data")
        found = tracklist.browsers_with_profile("win32", {}, self.root)
        self.assertEqual(found, ["brave"])

    def test_linux_finds_browsers_under_dot_config_and_dot_mozilla(self):
        self.make(".config/google-chrome", ".config/BraveSoftware/Brave-Browser", ".mozilla/firefox")
        found = tracklist.browsers_with_profile("linux", {}, self.root)
        self.assertEqual(found, ["chrome", "brave", "firefox"])

    def test_linux_honours_xdg_config_home(self):
        self.make("cfg/chromium")
        found = tracklist.browsers_with_profile("linux", {"XDG_CONFIG_HOME": str(self.root / "cfg")}, self.root)
        self.assertEqual(found, ["chromium"])

    def test_linux_finds_the_snap_builds_of_firefox_and_chromium(self):
        self.make("snap/firefox/common/.mozilla/firefox", "snap/chromium/common/chromium")
        found = tracklist.browsers_with_profile("linux", {}, self.root)
        self.assertEqual(found, ["firefox", "chromium"])

    def test_an_empty_machine_has_no_browsers(self):
        self.assertEqual(tracklist.browsers_with_profile("linux", {}, self.root), [])

    def test_safari_is_never_offered_off_macos(self):
        self.make("Library/Safari", ".config/safari")
        self.assertNotIn("safari", tracklist.browsers_with_profile("linux", {}, self.root))

    def test_off_macos_detection_looks_at_profile_folders_instead_of_app_bundles(self):
        with mock.patch.object(tracklist.sys, "platform", "win32"), \
                mock.patch.object(tracklist, "browsers_with_profile", return_value=["firefox"]):
            self.assertEqual(tracklist.detect_browsers(), ["firefox"])

    def test_on_macos_detection_looks_at_app_bundles(self):
        with mock.patch.object(tracklist.sys, "platform", "darwin"), \
                mock.patch.object(tracklist, "installed_browsers", return_value=["safari"]) as apps:
            self.assertEqual(tracklist.detect_browsers(), ["safari"])
        apps.assert_called_once_with(tracklist.APP_DIRS)


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

    def test_safari_blocked_by_permissions_is_named_so_a_safari_user_is_not_told_to_sign_in(self):
        def read(browser):
            if browser == "safari":
                raise SystemExit(tracklist.t("safari_disk_access"))
            return {}

        with self.assertRaises(SystemExit) as raised:
            tracklist.pick_browser(["chrome", "safari"], read)
        self.assertIn("studio.youtube.com", str(raised.exception.code))
        self.assertIn("Full Disk Access", str(raised.exception.code))
        self.assertIn("Your terminal app (Terminal, iTerm, Ghostty...)", str(raised.exception.code))

    def test_browser_flag_replaces_the_saved_choice(self):
        tracklist.CONFIG_FILE.parent.mkdir(parents=True)
        tracklist.CONFIG_FILE.write_text(json.dumps({"browser": "brave", "otra": 1}))
        tracklist.save_browser("chrome")
        self.assertEqual(json.loads(tracklist.CONFIG_FILE.read_text()), {"browser": "chrome", "otra": 1})


class BrowserFallbackTest(unittest.TestCase):
    install = BrowserTest.install

    def setUp(self):
        BrowserTest.setUp(self)
        original = tracklist.browser_flag
        self.addCleanup(setattr, tracklist, "browser_flag", original)
        tracklist.browser_flag = None
        tracklist.save_browser("brave")
        self.install("Brave Browser.app", "Safari.app")
        self.reads = []

        def read(browser):
            self.reads.append(browser)
            return {"SAPISID": "x", "__Secure-1PAPISID": "y", "__Secure-3PAPISID": "z"} if browser == "safari" else {}

        tracklist.browser_cookies = read

    def test_a_saved_browser_that_lost_its_session_gives_way_to_one_that_has_it(self):
        session = tracklist.fresh_session()
        self.assertIn("SAPISID=x", session["headers"]["cookie"])
        self.assertEqual(tracklist.current_browser(), "safari")

    def test_the_browser_that_took_over_is_read_only_once_so_the_keychain_asks_once(self):
        tracklist.fresh_session()
        self.assertEqual(self.reads, ["brave", "safari"])

    def test_without_another_signed_in_browser_the_saved_one_is_named_with_the_way_out(self):
        tracklist.browser_cookies = lambda browser: {}
        with self.assertRaises(SystemExit) as stop:
            tracklist.fresh_session()
        self.assertIn("brave", str(stop.exception.code))
        self.assertIn("--browser", str(stop.exception.code))
        self.assertEqual(tracklist.current_browser(), "brave")

    def test_a_browser_named_with_the_flag_is_never_swapped_for_another(self):
        tracklist.browser_flag = "brave"
        with self.assertRaises(SystemExit) as stop:
            tracklist.fresh_session()
        self.assertIn("brave", str(stop.exception.code))
        self.assertEqual(tracklist.current_browser(), "brave")


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


class FakeFfmpeg:
    def __init__(self, test, lines=(), returncode=0):
        self.test = test
        self.lines = lines
        self.returncode = returncode
        self.commands = []
        self.processes = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"mp3")
        process = FakeProcess(self.lines, self.returncode, "boom", kwargs["stderr"])
        self.processes.append(process)
        return process


class TrackedAudioTest(LocalAudio, unittest.TestCase):
    def fake_ffmpeg(self, lines=(), returncode=0):
        fake = FakeFfmpeg(self, lines, returncode)
        original = upload.subprocess.Popen
        upload.subprocess.Popen = fake
        self.addCleanup(setattr, upload.subprocess, "Popen", original)
        return fake

    def test_audio_conversion_reports_percent_from_ffmpeg(self):
        self.fake_ffmpeg(["out_time_us=30000000\n", "progress=continue\n"])
        events = []
        upload.place_audio(self.source, "vid12345678", 120, events.append)
        self.assertEqual(events, [("step", "audio", 25.0, None)])

    def test_conversion_progress_never_passes_a_hundred_percent(self):
        self.fake_ffmpeg(["out_time_us=999000000\n"])
        events = []
        upload.place_audio(self.source, "vid12345678", 120, events.append)
        self.assertEqual(events, [("step", "audio", 100, None)])

    def test_a_video_conversion_reports_under_its_own_name(self):
        self.fake_ffmpeg(["out_time_us=60000000\n"])
        events = []
        upload.convert_to_video(self.source, self.root / "out.mp4", 120, events.append)
        self.assertEqual(events, [("step", "convert", 50.0, None)])

    def test_without_a_duration_no_percent_is_invented(self):
        self.fake_ffmpeg(["out_time_us=30000000\n"])
        events = []
        upload.place_audio(self.source, "vid12345678", None, events.append)
        self.assertEqual(events, [])

    def test_a_cancelled_audio_conversion_leaves_no_mp3(self):
        self.fake_ffmpeg(["out_time_us=30000000\n"])
        stop = threading.Event()
        stop.set()
        with self.assertRaises(upload.Cancelled):
            upload.place_audio(self.source, "vid12345678", 120, stop=stop)
        target = tracklist.audio_path("vid12345678")
        self.assertEqual(list(target.parent.glob("vid12345678*")), [])

    def test_a_failed_audio_conversion_leaves_no_mp3(self):
        self.fake_ffmpeg([], returncode=1)
        with self.assertRaises(subprocess.CalledProcessError):
            upload.place_audio(self.source, "vid12345678", 120)
        target = tracklist.audio_path("vid12345678")
        self.assertEqual(list(target.parent.glob("vid12345678*")), [])

    def test_a_finished_audio_conversion_appears_only_when_complete(self):
        target = tracklist.audio_path("vid12345678")
        seen = []

        def report(event):
            seen.append(target.exists())

        self.fake_ffmpeg(["out_time_us=30000000\n"])
        upload.place_audio(self.source, "vid12345678", 120, report)
        self.assertEqual(seen, [False])
        self.assertTrue(target.exists())
        self.assertEqual(list(target.parent.glob("*.part.mp3")), [])

    def test_ffmpeg_is_asked_to_print_machine_readable_progress(self):
        fake = self.fake_ffmpeg()
        upload.place_audio(self.source, "vid12345678", 120)
        command = fake.commands[0]
        self.assertIn("-nostats", command)
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")


class FakeStatus:
    def __init__(self, fraction):
        self.fraction = fraction

    def progress(self):
        return self.fraction


class FakeChunkRequest:
    def __init__(self, statuses, response, on_chunk=None):
        self.steps = list(statuses) + [None]
        self.response = response
        self.on_chunk = on_chunk
        self.chunks = 0

    def next_chunk(self):
        status = self.steps[self.chunks]
        self.chunks += 1
        if self.on_chunk:
            self.on_chunk()
        if status is None:
            return None, self.response
        return FakeStatus(status), None


class FakeInsertClient:
    def __init__(self, request):
        self.request = request
        self.media = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.media = kwargs["media_body"]
        return self.request


class UploadVideoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "set.mp4"
        self.path.write_bytes(b"video")

    def test_upload_reports_progress_per_chunk(self):
        client = FakeInsertClient(FakeChunkRequest([0.25, 0.5], {"id": "abc"}))
        events = []
        self.assertEqual(upload.upload_video(client, self.path, "Set", events.append), "abc")
        self.assertEqual(events[1:], [("step", "upload", 25.0, None), ("step", "upload", 50.0, None)])

    def test_the_upload_is_sent_in_chunks_so_progress_and_cancel_can_happen(self):
        client = FakeInsertClient(FakeChunkRequest([], {"id": "abc"}))
        upload.upload_video(client, self.path, "Set")
        self.assertEqual(client.media.chunksize(), upload.UPLOAD_CHUNK)

    def test_an_upload_cancelled_before_it_starts_sends_nothing(self):
        stop = threading.Event()
        stop.set()
        request = FakeChunkRequest([0.25], {"id": "abc"})
        with self.assertRaises(upload.Cancelled):
            upload.upload_video(FakeInsertClient(request), self.path, "Set", stop=stop)
        self.assertEqual(request.chunks, 0)

    def test_the_upload_is_reported_as_started_before_the_first_chunk_is_sent(self):
        events, seen = [], []
        request = FakeChunkRequest([], {"id": "abc"}, lambda: seen.append(list(events)))
        upload.upload_video(FakeInsertClient(request), self.path, "Set", events.append)
        self.assertEqual(seen[0], [("step", "upload", 0.0, None)])

    def test_stopping_an_upload_stops_between_chunks(self):
        stop = threading.Event()
        request = FakeChunkRequest([0.25, 0.5], {"id": "abc"}, stop.set)
        with self.assertRaises(upload.Cancelled):
            upload.upload_video(FakeInsertClient(request), self.path, "Set", stop=stop)
        self.assertEqual(request.chunks, 1)


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


class FakeStdout:
    def __init__(self, lines, process):
        self.lines = lines
        self.process = process
        self.closed = False

    def __iter__(self):
        yield from self.lines
        if self.process.stalls:
            self.process.ended.wait(30)

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, lines=(), returncode=0, stderr="", stderr_file=None, stalls=False, ignores_terminate=False):
        self.stalls = stalls
        self.ignores_terminate = ignores_terminate
        self.ended = threading.Event()
        self.stdout = FakeStdout(lines, self)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.waited = False
        if stderr_file is not None:
            stderr_file.write(stderr)
            stderr_file.flush()

    def poll(self):
        return self.returncode if self.ended.is_set() else None

    def terminate(self):
        self.terminated = True
        if not self.ignores_terminate:
            self.ended.set()

    def kill(self):
        self.killed = True
        self.ended.set()

    def wait(self, timeout=None):
        self.waited = True
        self.ended.set()
        return self.returncode


class DownloadForeignTest(IsolatedCache, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.commands = []
        self.returncode = 0
        self.lines = []
        self.stderr = ""
        self.stalls = False
        self.ignores_terminate = False
        self.process = None
        original = (upload.subprocess.Popen, tracklist.current_browser)

        def fake_popen(command, **kwargs):
            self.commands.append(command)
            self.process = FakeProcess(self.lines, self.returncode, self.stderr, kwargs["stderr"], self.stalls, self.ignores_terminate)
            return self.process

        def restore():
            upload.subprocess.Popen, tracklist.current_browser = original

        self.addCleanup(restore)
        upload.subprocess.Popen = fake_popen
        tracklist.current_browser = lambda: "firefox"

    def download_events(self, lines):
        self.lines = lines
        events = []
        upload.download_foreign("vid12345678", events.append)
        return events

    def test_download_progress_reports_percent_and_speed(self):
        events = self.download_events(["  68.0%|  4.92MiB/s\n"])
        self.assertEqual(events, [("step", "download", 68.0, "4.92MiB/s")])

    def test_download_progress_survives_colour_codes(self):
        events = self.download_events(["\x1b[0;94m 68.0%\x1b[0m|\x1b[0;32m4.92MiB/s\x1b[0m\n"])
        self.assertEqual(events, [("step", "download", 68.0, "4.92MiB/s")])

    def test_download_without_a_known_size_reports_no_percent(self):
        events = self.download_events(["   N/A|  1.00MiB/s\n"])
        self.assertEqual(events, [("step", "download", None, "1.00MiB/s")])

    def test_a_download_already_cancelled_never_starts_yt_dlp(self):
        self.lines = ["  10.0%|  1MiB/s\n", "  20.0%|  1MiB/s\n"]
        stop = threading.Event()
        stop.set()
        with self.assertRaises(upload.Cancelled):
            upload.download_foreign("vid12345678", stop=stop)
        self.assertEqual(self.commands, [])

    def test_a_failed_yt_dlp_run_carries_its_error_text(self):
        self.returncode = 1
        self.stderr = "ERROR: video unavailable"
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            upload.run_tracked(["yt-dlp"], lambda line: None, None, None)
        self.assertEqual(raised.exception.stderr, "ERROR: video unavailable")

    def test_lines_that_are_not_progress_are_ignored(self):
        events = self.download_events(["[youtube] warming up\n", "  5.0%|  1MiB/s\n"])
        self.assertEqual(events, [("step", "download", 5.0, "1MiB/s")])

    def stop_from_another_thread(self):
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        self.addCleanup(timer.join)
        self.addCleanup(timer.cancel)
        return stop

    def test_stopping_a_stalled_download_kills_yt_dlp_even_if_it_prints_nothing(self):
        self.stalls = True
        started = time.monotonic()
        with self.assertRaises(upload.Cancelled):
            upload.download_foreign("vid12345678", stop=self.stop_from_another_thread())
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.process.terminated and self.process.waited)

    def test_a_stalled_download_that_ignores_terminate_is_killed(self):
        self.stalls = True
        self.ignores_terminate = True
        original = upload.TERMINATE_GRACE
        upload.TERMINATE_GRACE = 0.2
        self.addCleanup(setattr, upload, "TERMINATE_GRACE", original)
        started = time.monotonic()
        with self.assertRaises(upload.Cancelled):
            upload.download_foreign("vid12345678", stop=self.stop_from_another_thread())
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.process.terminated and self.process.killed)

    def test_a_failing_progress_callback_still_reaps_the_child(self):
        self.lines = ["  5.0%|  1MiB/s\n"]

        def broken(event):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            upload.download_foreign("vid12345678", broken)
        self.assertTrue(self.process.terminated and self.process.waited)
        self.assertTrue(self.process.stdout.closed)

    def test_stopping_during_a_download_kills_yt_dlp_after_the_current_line(self):
        self.lines = ["  10.0%|  1MiB/s\n", "  20.0%|  1MiB/s\n"]
        stop = threading.Event()
        events = []

        def report(event):
            events.append(event)
            stop.set()

        with self.assertRaises(upload.Cancelled):
            upload.download_foreign("vid12345678", report, stop)
        self.assertEqual(len(events), 1)
        self.assertTrue(self.process.terminated and self.process.waited)

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


class WaitForClaimsTest(IsolatedCache, unittest.TestCase):
    def wait(self, counts, session=True, age=0):
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
            upload.wait_for_claims("vid12345678", time.clock() - age, messages.append, time.sleep, time.clock)
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

    def test_timeout_says_it_continues_and_late_claims_come_on_relaunch(self):
        polls = upload.CLAIMS_TIMEOUT // upload.CLAIMS_POLL + 1
        time, messages = self.wait([0] * polls)
        self.assertEqual(time.now, upload.CLAIMS_TIMEOUT)
        self.assertEqual(
            messages[-1],
            "Content ID took over an hour: continuing with what it found. "
            "Run the same command later to pick up late claims",
        )

    def test_a_copy_uploaded_over_an_hour_ago_does_not_wait(self):
        time, _ = self.wait([4], age=upload.CLAIMS_TIMEOUT)
        self.assertEqual(time.sleeps, [])
        self.assertTrue(tracklist.load_cache("vid12345678")["content_id_done"])

    def test_elapsed_counts_from_the_upload(self):
        _, messages = self.wait([3, 7, 7, 7], age=600)
        self.assertIn("10:00", messages[0])
        self.assertIn("16:00", messages[-1])

    def test_settling_marks_content_id_done(self):
        self.wait([3, 7, 7, 7])
        self.assertTrue(tracklist.load_cache("vid12345678")["content_id_done"])

    def test_a_negative_elapsed_counts_as_zero(self):
        _, messages = self.wait([3, 7, 7, 7], age=-500)
        self.assertIn("00:00", messages[0])

    def test_claims_elapsed_never_goes_negative(self):
        self.assertEqual(upload.claims_elapsed(1000, 400), 0)
        self.assertEqual(upload.claims_elapsed(100, 400.9), 300)


class WaitForProcessingTest(unittest.TestCase):
    def wait(self, statuses):
        polls = iter(statuses)
        original = upload.processing_details
        upload.processing_details = lambda client, video_id: (next(polls), 1793872800.0)
        time = FakeTime()
        try:
            uploaded_at = upload.wait_for_processing("client", "vid12345678", lambda message: None, time.sleep)
        finally:
            upload.processing_details = original
        self.uploaded_at = uploaded_at
        return time

    def test_claims_are_only_read_after_youtube_finishes_processing(self):
        time = self.wait(["processing", "processing", "succeeded"])
        self.assertEqual(time.sleeps, [upload.PROCESSING_POLL] * 2)

    def test_a_failed_upload_stops_instead_of_waiting_forever(self):
        with self.assertRaises(SystemExit):
            self.wait(["processing", "failed"])

    def test_the_upload_time_is_handed_on_to_the_claims_wait(self):
        self.wait(["processing", "succeeded"])
        self.assertEqual(self.uploaded_at, 1793872800.0)

    def test_processing_details_reads_status_and_upload_time(self):
        client = mock.Mock()
        client.videos().list().execute.return_value = {"items": [{
            "snippet": {"publishedAt": "2026-11-05T10:00:00Z"},
            "processingDetails": {"processingStatus": "succeeded"},
        }]}
        self.assertEqual(upload.processing_details(client, "vid12345678"), ("succeeded", 1793872800.0))

    def test_a_video_youtube_does_not_list_yet_counts_as_processing_since_now(self):
        client = mock.Mock()
        client.videos().list().execute.return_value = {}
        before = time.time()
        status, uploaded_at = upload.processing_details(client, "vid12345678")
        self.assertEqual(status, "processing")
        self.assertGreaterEqual(uploaded_at, before)

    def test_an_unreadable_upload_time_counts_as_now(self):
        client = mock.Mock()
        client.videos().list().execute.return_value = {"items": [{
            "snippet": {"publishedAt": "ayer"},
            "processingDetails": {"processingStatus": "succeeded"},
        }]}
        before = time.time()
        self.assertGreaterEqual(upload.processing_details(client, "vid12345678")[1], before)

    def test_a_terminated_upload_stops_instead_of_waiting_forever(self):
        with self.assertRaises(SystemExit):
            self.wait(["processing", "terminated"])


class WaitUntilReadyTest(IsolatedCache, unittest.TestCase):
    def test_a_finished_wait_skips_youtube(self):
        tracklist.mark_content_id_done("vid12345678")
        original = upload.youtube_client

        def refuse():
            raise AssertionError("youtube_client must not be called")

        upload.youtube_client = refuse
        self.addCleanup(setattr, upload, "youtube_client", original)
        self.assertFalse(upload.wait_until_ready("vid12345678", lambda message: None))

    def test_an_unfinished_wait_processes_then_reads_claims_from_the_upload_time(self):
        names = ("youtube_client", "wait_for_processing", "wait_for_claims")
        original = {name: getattr(upload, name) for name in names}
        calls = []
        upload.youtube_client = lambda: "client"
        upload.wait_for_processing = lambda client, video_id, report: calls.append("processing") or 42.0
        upload.wait_for_claims = lambda video_id, uploaded_at, report: calls.append(("claims", uploaded_at))
        self.addCleanup(lambda: [setattr(upload, n, v) for n, v in original.items()])
        self.assertTrue(upload.wait_until_ready("vid12345678", lambda message: None))
        self.assertEqual(calls, ["processing", ("claims", 42.0)])


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


class NeedsWaitTest(IsolatedUploads, IsolatedCache, unittest.TestCase):
    def test_a_remembered_copy_still_needs_the_wait(self):
        upload.remember("youtube:orig1234567", "copia123456")
        self.assertTrue(upload.needs_wait("copia123456"))

    def test_a_copy_whose_wait_finished_does_not_need_it_again(self):
        upload.remember("youtube:orig1234567", "copia123456")
        tracklist.mark_content_id_done("copia123456")
        self.assertFalse(upload.needs_wait("copia123456"))

    def test_my_own_video_never_waits(self):
        self.assertFalse(upload.needs_wait("orig1234567"))


class WaitInMainTest(IsolatedUploads, IsolatedCache, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.deleted = []

    def run_main(self, ready, *flags, video="https://www.youtube.com/watch?v=tZ6YRoNjvPI"):
        upload.remember("youtube:tZ6YRoNjvPI", "copia123456")
        with mock.patch.object(tracklist, "missing_tools", lambda: []), \
                fake_job("copia123456", "Set", True), \
                mock.patch.object(upload, "wait_until_ready", ready), \
                mock.patch.object(upload, "offer_delete", self.deleted.append), \
                mock.patch.object(tracklist, "try_claims", lambda video_id, own: (None, [], None)), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch("sys.argv", ["tracklist", video, "--no-recognize", "--duration", "600", *flags]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            tracklist.main()

    def test_fresh_on_a_finished_copy_waits_again(self):
        tracklist.mark_content_id_done("copia123456")
        calls = []
        self.run_main(lambda video_id, report: calls.append(video_id) or True, "--fresh")
        self.assertEqual(calls, ["copia123456"])

    def test_a_finished_copy_does_not_wait_on_relaunch(self):
        tracklist.mark_content_id_done("copia123456")
        calls = []
        self.run_main(lambda video_id, report: calls.append(video_id) or True)
        self.assertEqual(calls, [])

    def test_ctrl_c_during_the_wait_tells_how_to_resume_and_offers_no_delete(self):
        def interrupted(video_id, report):
            raise KeyboardInterrupt

        with self.assertRaises(SystemExit) as raised:
            self.run_main(interrupted)
        self.assertIn("run 'tracklist https://www.youtube.com/watch?v=tZ6YRoNjvPI' again", str(raised.exception.code))
        self.assertEqual(self.deleted, [])


def fake_job(copy, title, own):
    return mock.patch.multiple(
        upload,
        plan_job=lambda *args, **kwargs: upload.Job("orig1234567", title, None, copy, own, None),
        run_steps=lambda job, progress, stop: upload.Outcome(copy, None, own, None),
    )


class StepFakes(IsolatedUploads, IsolatedCache):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.uploaded_titles = []
        self.quota_error = False
        self.owner = (False, "Set ajeno")
        self.client_error = None
        self.reports = []
        self.download_result = "file"

        def client():
            if self.client_error:
                raise self.client_error
            self.calls.append("client")
            return "client"

        def fake_download(video_id, progress=None, stop=None):
            self.calls.append("download")
            if self.download_result is None:
                return None
            upload.downloads_dir().mkdir(parents=True, exist_ok=True)
            path = upload.downloads_dir() / f"{video_id}.webm"
            path.write_bytes(b"audio")
            return path

        def fake_convert(source, target, duration=None, progress=None, stop=None):
            self.calls.append(("convert", duration))
            target.write_bytes(b"mp4")

        def fake_upload(client, path, title, progress=None, stop=None):
            self.calls.append("upload")
            if self.quota_error:
                raise upload.QuotaExceeded()
            self.uploaded_titles.append(title)
            return "copia123456"

        def fake_place(source, video_id, duration=None, progress=None, stop=None):
            self.calls.append(("place", video_id, duration))

        patches = [
            mock.patch.object(upload, "youtube_client", client),
            mock.patch.object(upload, "video_owner", lambda client, video_id: self.owner),
            mock.patch.object(upload, "download_foreign", fake_download),
            mock.patch.object(upload, "convert_to_video", fake_convert),
            mock.patch.object(upload, "upload_video", fake_upload),
            mock.patch.object(upload, "place_audio", fake_place),
            mock.patch.object(upload, "local_duration", lambda source: 600),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class PlanJobTest(StepFakes, unittest.TestCase):
    def plan(self, video_id="orig1234567", local=None):
        return upload.plan_job(video_id, local, self.reports.append)

    def test_my_own_video_is_read_directly_with_nothing_to_run(self):
        self.owner = (True, "Mi set")
        job = self.plan()
        self.assertEqual((job.copy, job.own, job.audio, job.title), ("orig1234567", True, None, None))
        self.assertEqual(self.calls, ["client"])

    def test_a_foreign_video_without_a_copy_still_has_its_upload_ahead(self):
        job = self.plan()
        self.assertEqual((job.original_id, job.copy, job.own, job.title), ("orig1234567", None, True, "Set ajeno"))
        self.assertEqual(self.calls, ["client"])

    def test_a_copy_already_uploaded_is_reused_even_after_the_download_was_deleted(self):
        upload.remember(upload.remote_key("orig1234567"), "copia123456")
        job = self.plan()
        self.assertEqual((job.copy, job.own, job.audio, job.title), ("copia123456", True, None, "Set ajeno"))
        self.assertIn("Already uploaded as https://youtu.be/copia123456", self.reports)

    def test_a_known_copy_hands_over_the_download_that_is_still_on_disk(self):
        upload.remember(upload.remote_key("orig1234567"), "copia123456")
        upload.downloads_dir().mkdir(parents=True)
        kept = upload.downloads_dir() / "orig1234567.webm"
        kept.write_bytes(b"audio")
        self.assertEqual(self.plan().audio, kept)

    def test_a_new_local_recording_is_its_own_audio(self):
        source = self.root / "Spook Multiespacio 3.m4a"
        source.write_bytes(b"audio")
        job = self.plan(None, source)
        self.assertEqual((job.local, job.audio, job.copy, job.own), (source, source, None, True))

    def test_a_local_recording_is_titled_after_its_file_since_that_heads_the_tracklist(self):
        source = self.root / "Spook Multiespacio 3.m4a"
        source.write_bytes(b"audio")
        self.assertEqual(self.plan(None, source).title, "Spook Multiespacio 3")

    def test_a_local_recording_uploaded_before_is_not_uploaded_again(self):
        source = self.root / "Spook Multiespacio 3.m4a"
        source.write_bytes(b"audio")
        upload.remember_upload(source, "vid12345678")
        job = self.plan(None, source)
        self.assertEqual(job.copy, "vid12345678")
        self.assertIn("Already uploaded as https://youtu.be/vid12345678", self.reports)

    def test_without_a_google_login_it_keeps_working_with_shazam_alone(self):
        self.client_error = SystemExit("Falta la credencial")
        job = self.plan()
        self.assertEqual((job.original_id, job.copy, job.own), ("orig1234567", None, False))
        self.assertTrue(any("Shazam only" in message for message in self.reports))

    def test_an_account_without_a_channel_keeps_working_with_shazam_alone(self):
        def no_channel(client, video_id):
            raise SystemExit("Tu cuenta de Google no tiene canal de YouTube")

        upload.video_owner = no_channel
        job = self.plan()
        self.assertEqual((job.copy, job.own), (None, False))
        self.assertTrue(any("Shazam only" in message for message in self.reports))

    def test_an_api_error_while_checking_ownership_falls_back_to_shazam(self):
        from googleapiclient.errors import HttpError

        def failing(client, video_id):
            raise HttpError(mock.Mock(status=403, reason="quota"), b"quotaExceeded")

        upload.video_owner = failing
        job = self.plan()
        self.assertEqual((job.copy, job.own), (None, False))
        self.assertTrue(any("Shazam only" in message for message in self.reports))


class RunStepsTest(StepFakes, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.source = self.root / "Spook Multiespacio 3.m4a"
        self.source.write_bytes(b"audio")

    def foreign(self, **changes):
        fields = dict(original_id="orig1234567", title="Set ajeno", local=None, copy=None, own=True, audio=None)
        return upload.Job(**{**fields, **changes})

    def local(self, **changes):
        fields = dict(original_id=None, title=None, local=self.source, copy=None, own=True, audio=self.source)
        return upload.Job(**{**fields, **changes})

    def run_steps(self, job):
        return upload.run_steps(job, self.events.append, threading.Event())

    def test_a_foreign_video_is_downloaded_converted_uploaded_and_placed_in_order(self):
        outcome = self.run_steps(self.foreign())
        self.assertEqual(self.calls, ["download", ("convert", 600), "client", "upload", ("place", "copia123456", 600)])
        self.assertEqual(self.uploaded_titles, ["Set ajeno"])
        self.assertEqual(outcome, upload.Outcome("copia123456", upload.downloads_dir() / "orig1234567.webm", True, 600))
        self.assertIn(("step-done", "upload", "copia123456"), self.events)

    def test_the_upload_is_remembered_before_the_audio_is_placed_so_a_crash_does_not_repeat_it(self):
        def crash(*args, **kwargs):
            raise RuntimeError("disk full")

        upload.place_audio = crash
        with self.assertRaises(RuntimeError):
            self.run_steps(self.foreign())
        self.assertEqual(upload.load_uploads()[upload.remote_key("orig1234567")], "copia123456")

    def test_a_known_copy_with_its_audio_only_places_the_audio(self):
        audio = self.root / "orig.webm"
        audio.write_bytes(b"audio")
        outcome = self.run_steps(self.foreign(copy="copia123456", audio=audio))
        self.assertEqual(self.calls, [("place", "copia123456", 600)])
        self.assertEqual(outcome, upload.Outcome("copia123456", audio, True, 600))

    def test_a_known_copy_without_audio_runs_nothing(self):
        outcome = self.run_steps(self.foreign(copy="copia123456"))
        self.assertEqual((self.calls, outcome), ([], upload.Outcome("copia123456", None, True, None)))

    def test_my_own_video_runs_nothing(self):
        outcome = self.run_steps(self.foreign(copy="orig1234567"))
        self.assertEqual((self.calls, outcome), ([], upload.Outcome("orig1234567", None, True, None)))

    def test_a_shazam_only_job_runs_nothing(self):
        outcome = self.run_steps(self.foreign(own=False))
        self.assertEqual((self.calls, outcome), ([], upload.Outcome("orig1234567", None, False, None)))

    def test_a_video_that_cannot_be_downloaded_falls_back_to_shazam(self):
        self.download_result = None
        outcome = self.run_steps(self.foreign())
        self.assertEqual(outcome, upload.Outcome("orig1234567", None, False, None))
        self.assertIn(("warning", "Could not download the video: continuing with Shazam only"), self.events)
        self.assertEqual(self.calls, ["download"])

    def test_an_exhausted_quota_falls_back_to_shazam_but_keeps_the_download(self):
        self.quota_error = True
        outcome = self.run_steps(self.foreign())
        self.assertEqual(outcome, upload.Outcome("orig1234567", upload.downloads_dir() / "orig1234567.webm", False, 600))
        self.assertIn(("warning", "YouTube upload quota used up: continuing with Shazam only"), self.events)
        self.assertEqual(upload.load_uploads(), {})

    def test_an_exhausted_quota_still_places_the_download_so_shazam_does_not_fetch_it_again(self):
        self.quota_error = True
        self.run_steps(self.foreign())
        self.assertIn(("place", "orig1234567", 600), self.calls)

    def test_an_exhausted_quota_stops_a_local_recording(self):
        self.quota_error = True
        with self.assertRaises(SystemExit) as raised:
            self.run_steps(self.local())
        self.assertIn("quota used up", str(raised.exception.code))
        self.assertIsNone(upload.known_upload(self.source))

    def test_a_local_recording_is_titled_after_the_file_and_remembered(self):
        outcome = self.run_steps(self.local())
        self.assertEqual(self.uploaded_titles, ["Spook Multiespacio 3"])
        self.assertEqual(outcome, upload.Outcome("copia123456", self.source, True, 600))
        self.assertEqual(upload.known_upload(self.source), "copia123456")

    def test_a_known_local_copy_only_places_the_audio(self):
        self.run_steps(self.local(copy="vid12345678"))
        self.assertEqual(self.calls, [("place", "vid12345678", 600)])

    def test_deleting_the_uploaded_copy_also_forgets_it_and_deletes_the_download(self):
        outcome = self.run_steps(self.foreign())
        self.assertTrue(outcome.audio.exists())
        upload.forget_upload(outcome.video_id)
        self.assertFalse(outcome.audio.exists())
        self.assertEqual(upload.load_uploads(), {})

    def test_cancelling_stops_the_steps_and_remembers_nothing(self):
        def cancelled(*args, **kwargs):
            raise upload.Cancelled()

        upload.upload_video = cancelled
        with self.assertRaises(upload.Cancelled):
            self.run_steps(self.foreign())
        self.assertEqual(upload.load_uploads(), {})


class TerminalProgressTest(unittest.TestCase):
    class Stream(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_a_terminal_rewrites_one_line_and_ends_it_before_the_next_message(self):
        stream = self.Stream(True)
        progress = tracklist.terminal_progress(stream)
        progress(("step", "upload", 10.0, None))
        progress(("step", "upload", 55.0, None))
        progress(("step-done", "upload", "copia123456"))
        text = stream.getvalue()
        self.assertEqual(text.count("\r"), 2)
        self.assertIn("55%", text)
        self.assertIn("\nUploaded as https://youtu.be/copia123456\n", text)

    def test_a_terminal_starts_a_new_line_when_the_step_changes(self):
        stream = self.Stream(True)
        progress = tracklist.terminal_progress(stream)
        progress(("step", "convert", 100.0, None))
        progress(("step", "upload", 0.0, None))
        self.assertIn("100%\x1b[K\n\r", stream.getvalue())

    def test_the_detail_is_shown_next_to_the_percent(self):
        stream = self.Stream(True)
        tracklist.terminal_progress(stream)(("step", "download", 40.0, "2.1MiB/s"))
        self.assertIn("40% · 2.1MiB/s", stream.getvalue())

    def test_finishing_ends_a_line_still_being_rewritten(self):
        stream = self.Stream(True)
        progress = tracklist.terminal_progress(stream)
        progress(("step", "audio", 99.0, None))
        progress.finish()
        self.assertTrue(stream.getvalue().endswith("\n"))

    def test_a_pipe_gets_one_line_per_step_not_one_per_percent(self):
        stream = self.Stream(False)
        progress = tracklist.terminal_progress(stream)
        for percent in (1.0, 50.0, 100.0):
            progress(("step", "convert", percent, None))
        progress(("step", "upload", 3.0, None))
        self.assertEqual(stream.getvalue(), "Converting to video\nUploading to YouTube\n")

    def test_the_audio_step_is_labelled_like_in_the_tui(self):
        stream = self.Stream(False)
        tracklist.terminal_progress(stream)(("step", "audio", 1.0, None))
        self.assertEqual(stream.getvalue(), "Preparing the audio\n")

    def test_only_a_finished_upload_says_uploaded(self):
        stream = self.Stream(False)
        tracklist.terminal_progress(stream)(("step-done", "convert", "x"))
        self.assertEqual(stream.getvalue(), "")

    def test_it_writes_to_the_stderr_in_place_when_called(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            tracklist.terminal_progress()(("warning", "Careful"))
        self.assertEqual(errors.getvalue(), "Careful\n")

    def test_warnings_get_their_own_line(self):
        stream = self.Stream(True)
        progress = tracklist.terminal_progress(stream)
        progress(("step", "download", 5.0, None))
        progress(("warning", "Careful"))
        self.assertTrue(stream.getvalue().endswith("\nCareful\n"))


class RunStepsInMainTest(StepFakes, unittest.TestCase):
    def run_main(self, *flags, video="https://www.youtube.com/watch?v=orig1234567", duration=("--duration", "600")):
        self.deleted = []
        errors = io.StringIO()
        with mock.patch.object(tracklist, "missing_tools", lambda: []), \
                mock.patch.object(upload, "offer_delete", self.deleted.append), \
                mock.patch.object(upload, "wait_until_ready", lambda video_id, report: False), \
                mock.patch.object(tracklist, "try_claims", lambda video_id, own: (None, [], None)), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch("sys.argv", ["tracklist", video, "--no-recognize", *duration, *flags]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            tracklist.main()
        return errors.getvalue()

    def test_relaunching_with_a_known_copy_neither_downloads_nor_uploads(self):
        upload.remember(upload.remote_key("orig1234567"), "copia123456")
        self.run_main()
        self.assertNotIn("download", self.calls)
        self.assertNotIn("upload", self.calls)

    def test_ctrl_c_during_the_steps_tells_how_to_resume_and_offers_no_delete(self):
        def interrupted(job, progress, stop):
            raise KeyboardInterrupt

        with mock.patch.object(upload, "run_steps", interrupted), self.assertRaises(SystemExit) as raised:
            self.run_main()
        self.assertIn("Stopped before finishing: run 'tracklist https://www.youtube.com/watch?v=orig1234567' again to continue", str(raised.exception.code))
        self.assertEqual(self.deleted, [])

    def test_fresh_clears_the_cache_of_the_known_copy(self):
        upload.remember(upload.remote_key("orig1234567"), "copia123456")
        tracklist.mark_content_id_done("copia123456")
        self.run_main("--fresh")
        self.assertFalse(tracklist.load_cache("copia123456")["content_id_done"])

    def test_a_saved_claims_file_still_prepares_a_local_recording(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        saved = self.root / "claims.json"
        saved.write_text(json.dumps({"receivedClaims": [{
            "matchDetails": {"longestMatchStartTimeSeconds": 0, "longestMatchDurationSeconds": 300},
            "asset": {"metadata": {"soundRecording": {"title": "T", "artists": ["A"]}}},
        }]}))
        self.run_main("--from-json", str(saved), video=str(source))
        self.assertIn("upload", self.calls)

    def test_the_duration_of_the_audio_is_used_when_none_is_given_and_claims_say_nothing(self):
        upload.remember(upload.remote_key("orig1234567"), "copia123456")
        upload.downloads_dir().mkdir(parents=True)
        (upload.downloads_dir() / "orig1234567.webm").write_bytes(b"audio")
        upload.local_duration = lambda source: 1234
        seen = []
        with mock.patch.object(tracklist, "find_gaps", lambda entries, duration: seen.append(duration) or []), \
                mock.patch.object(tracklist, "scan_gaps", lambda duration: seen.append(duration) or []):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(duration=())
        self.assertEqual(raised.exception.code, messages.t("too_short"))
        self.assertEqual(seen, [1234])


class TracklistPointsToOriginalTest(unittest.TestCase):
    def test_the_tracklist_points_to_the_original_video_not_the_copy(self):
        output = io.StringIO()
        with fake_job("copia123456", "Set", True), \
                mock.patch.object(upload, "offer_delete", lambda video_id: None), \
                mock.patch.object(tracklist, "missing_tools", lambda: []), \
                mock.patch.object(tracklist, "try_claims", lambda video_id, own: (None, [{"start": 0, "duration": 60, "title": "Tema", "artist": "A"}], None)), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch("sys.argv", ["tracklist", "orig1234567", "--no-recognize", "--duration", "600"]), \
                contextlib.redirect_stdout(output):
            tracklist.main()
        self.assertIn("watch?v=orig1234567", output.getvalue())
        self.assertIn("Set", output.getvalue())
        self.assertNotIn("copia123456", output.getvalue())


class FromJsonTest(unittest.TestCase):
    def test_a_saved_claims_response_never_asks_google_or_uploads_anything(self):
        claim = {"receivedClaims": [{
            "matchDetails": {"longestMatchStartTimeSeconds": 0, "longestMatchDurationSeconds": 300},
            "asset": {"metadata": {"soundRecording": {"title": "Kisses of Fire", "artists": ["La Luna"]}}},
        }]}
        planned = []
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / "claims.json"
            saved.write_text(json.dumps(claim))
            with mock.patch.object(tracklist, "missing_tools", lambda: []), \
                    mock.patch.object(upload, "plan_job", lambda *args, **kwargs: planned.append(args)), \
                    mock.patch.object(upload, "offer_delete", lambda video_id: None), \
                    mock.patch("sys.argv", ["tracklist", "orig1234567", "--from-json", str(saved),
                                            "--no-recognize", "--duration", "600"]), \
                    contextlib.redirect_stdout(output):
                tracklist.main()
        self.assertEqual(planned, [])
        self.assertIn("La Luna - Kisses of Fire", output.getvalue())
        self.assertIn("watch?v=orig1234567", output.getvalue())


class UpdateNoticeTest(unittest.TestCase):
    INSTALLED = "a" * 40
    NEWER = "b" * 40

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.original = (tracklist.CONFIG_FILE, tracklist.update_notice, tracklist.installed_commit, tracklist.latest_commit)
        tracklist.CONFIG_FILE = Path(tmp.name) / "config.json"
        self.fetches = 0
        self.latest = self.NEWER
        self.installed = self.INSTALLED
        self.clock = 1_000_000

        def fetch():
            self.fetches += 1
            return self.latest

        tracklist.update_notice = self.original[1]
        tracklist.installed_commit = lambda: self.installed
        tracklist.latest_commit = fetch
        self.addCleanup(self.restore)

    def restore(self):
        tracklist.CONFIG_FILE, tracklist.update_notice, tracklist.installed_commit, tracklist.latest_commit = self.original

    def notice(self):
        with mock.patch.object(tracklist.time, "time", lambda: self.clock):
            return tracklist.update_notice()

    def test_a_newer_version_on_github_points_to_the_update_command(self):
        self.assertIn("tracklist update", self.notice())

    def test_the_notice_follows_the_system_language(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertIn("version nueva", self.notice())

    def test_being_up_to_date_says_nothing(self):
        self.latest = self.INSTALLED
        self.assertIsNone(self.notice())

    def test_a_checkout_that_was_not_installed_from_git_never_looks_online(self):
        self.installed = None
        self.assertIsNone(self.notice())
        self.assertEqual(self.fetches, 0)

    def test_github_is_asked_once_a_day_so_opening_stays_instant(self):
        self.notice()
        self.clock += 3600
        self.assertIsNone(self.notice())
        self.assertEqual(self.fetches, 1)

    def test_the_next_day_it_looks_again(self):
        self.notice()
        self.clock += 24 * 3600
        self.assertIn("tracklist update", self.notice())
        self.assertEqual(self.fetches, 2)

    def test_being_offline_says_nothing_and_is_not_retried_on_every_run(self):
        self.latest = None
        self.assertIsNone(self.notice())
        self.assertIsNone(self.notice())
        self.assertEqual(self.fetches, 1)

    def test_checking_keeps_the_saved_browser(self):
        tracklist.save_browser("brave")
        self.notice()
        self.assertEqual(tracklist.load_config()["browser"], "brave")

    def test_the_installed_commit_is_the_one_uv_recorded(self):
        record = json.dumps({"vcs_info": {"vcs": "git", "commit_id": self.INSTALLED}})
        with mock.patch("importlib.metadata.distribution", return_value=mock.Mock(read_text=lambda name: record)):
            self.assertEqual(self.original[2](), self.INSTALLED)

    def test_without_a_vcs_record_there_is_no_installed_commit(self):
        with mock.patch("importlib.metadata.distribution", return_value=mock.Mock(read_text=lambda name: None)):
            self.assertIsNone(self.original[2]())

    def test_a_network_failure_leaves_the_latest_commit_unknown(self):
        with mock.patch.object(tracklist.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertIsNone(self.original[3]())

    def test_the_latest_commit_is_read_from_the_github_api(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = f"{self.NEWER}\n".encode()
        with mock.patch.object(tracklist.urllib.request, "urlopen", return_value=response):
            self.assertEqual(self.original[3](), self.NEWER)

    def test_opening_prints_the_notice_before_anything_else(self):
        errors = io.StringIO()
        with mock.patch("sys.argv", ["tracklist"]), \
                mock.patch.object(tracklist, "missing_tools", lambda: []), \
                mock.patch.object(tracklist, "update_notice", lambda: "NOTICE"), \
                contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            tracklist.main()
        self.assertTrue(errors.getvalue().startswith("NOTICE"))


class UpdateCommandTest(unittest.TestCase):
    def update(self, result=None, error=None):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if error:
                raise error
            return result

        with mock.patch("sys.argv", ["tracklist", "update"]), \
                mock.patch.object(tracklist, "missing_tools", lambda: ["yt-dlp"]), \
                mock.patch.object(tracklist.subprocess, "run", fake_run):
            try:
                tracklist.main()
                code = None
            except SystemExit as stop:
                code = stop.code
        return calls, code

    def test_update_reinstalls_the_latest_version_from_github_with_uv(self):
        calls, code = self.update(mock.Mock(returncode=0))
        self.assertEqual(calls, [["uv", "tool", "install", "--force", "git+https://github.com/vigosan/tracklist"]])
        self.assertIsNone(code)

    def test_a_failed_install_exits_with_the_status_uv_reported(self):
        _, code = self.update(mock.Mock(returncode=2))
        self.assertEqual(code, 2)

    def test_without_uv_it_says_how_to_get_it(self):
        _, code = self.update(error=FileNotFoundError())
        self.assertIn("brew install uv", code)


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
            result = recognize_tui.run_app("dnS4s0gar-4", 600, False)
        self.assertEqual([entry["title"] for entry in result.entries], ["Kisses of Fire"])
        self.assertEqual(result.gaps, [(300, 600)])


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


class TuiWaitTest(IsolatedCache, unittest.TestCase):
    CLAIMS = [
        {"artist": "La Luna", "title": "Kisses of Fire", "label": "", "start": 0, "duration": 60},
        {"artist": "Cosmo", "title": "Wave", "label": "", "start": 60, "duration": 60},
    ]

    def run_wait(self, statuses, counts, until_done=True, steps=None, waiting=True, failure=None):
        from textual.app import App

        processing = iter(statuses)
        polls = iter(counts)
        recognized = []
        self.checked = []
        self.notified = []

        def details(client, video_id):
            self.checked.append(video_id)
            return next(processing), time.time()

        async def recognition(video_id, gaps, progress, resume=None):
            recognized.append(gaps)
            return []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test() as pilot:
                    if until_done:
                        await app.workers.wait_for_complete()
                    else:
                        await pilot.pause(0.2)
                    if steps:
                        await steps(app, pilot)
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(upload, "youtube_client", lambda: "client"), \
                mock.patch.object(upload, "processing_details", details), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch.object(tracklist, "probe_duration", lambda video_id: 600), \
                mock.patch.object(upload, "PROCESSING_POLL", 0), \
                mock.patch.object(upload, "CLAIMS_POLL", 0), \
                mock.patch.object(tracklist, "try_claims", lambda video_id, own: ({}, self.CLAIMS[:next(polls, counts[-1] if counts else 0)], failure)), \
                mock.patch.object(tracklist, "recognize_gaps_async", recognition), \
                mock.patch.object(tracklist, "notify_ready", self.notified.append), \
                mock.patch.object(App, "run", drive):
            result = recognize_tui.run_app("copia123456", 600, False, True, waiting=waiting)
        return result, recognized

    def test_claims_appear_during_the_wait_and_shazam_starts_after(self):
        result, recognized = self.run_wait(["processing", "succeeded"], [0, 2, 2, 2])
        self.assertEqual(len(result.entries), 2)
        self.assertIsNone(result.error)
        self.assertFalse(result.interrupted)
        self.assertTrue(tracklist.load_cache("copia123456")["content_id_done"])
        self.assertEqual(recognized, [[(120, 600)]])

    def test_a_finished_wait_notifies_once_even_after_a_refresh(self):
        async def refresh(app, pilot):
            await pilot.press("r")
            await pilot.pause()
            await app.workers.wait_for_complete()

        self.run_wait(["processing", "succeeded"], [2, 2, 2], steps=refresh)
        self.assertEqual(len(self.notified), 1)

    def test_no_notification_when_there_was_no_wait(self):
        self.run_wait([], [2, 2, 2], waiting=False)
        self.assertEqual(self.notified, [])

    def test_no_notification_when_the_wait_is_interrupted_or_fails(self):
        self.run_wait(iter(lambda: "processing", None), [], until_done=False)
        self.assertEqual(self.notified, [])
        self.run_wait(["failed"], [])
        self.assertEqual(self.notified, [])

    def test_unreadable_studio_claims_during_the_wait_close_the_app_with_the_error(self):
        result, recognized = self.run_wait(["succeeded"], [0], failure="Studio session expired")
        self.assertIn("Studio session expired", result.error)
        self.assertFalse(result.interrupted)
        self.assertEqual(recognized, [])
        self.assertFalse(tracklist.load_cache("copia123456")["content_id_done"])

    def test_quitting_during_the_wait_reports_interrupted(self):
        result, recognized = self.run_wait(iter(lambda: "processing", None), [], until_done=False)
        self.assertTrue(result.interrupted)
        self.assertEqual(recognized, [])
        self.assertFalse(tracklist.load_cache("copia123456")["content_id_done"])

    def test_typing_another_url_during_the_wait_still_counts_as_leaving_mid_wait(self):
        async def type_url(app, pilot):
            await pilot.press("n")
            app.query_one("#video-input").value = "dnS4s0gar-5"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()

        result, _ = self.run_wait(iter(lambda: "processing", None), [], until_done=False, steps=type_url)
        self.assertTrue(result.interrupted)
        self.assertFalse(tracklist.load_cache("copia123456")["content_id_done"])

    def test_refreshing_after_a_finished_wait_does_not_wait_again(self):
        async def refresh(app, pilot):
            await pilot.press("r")
            await pilot.pause()
            await app.workers.wait_for_complete()

        result, recognized = self.run_wait(["processing", "succeeded"], [2, 2, 2], steps=refresh)
        self.assertEqual(self.checked, ["copia123456", "copia123456"])
        self.assertEqual(len(recognized), 2)
        self.assertFalse(result.interrupted)

    def test_a_failed_processing_closes_with_the_error(self):
        result, recognized = self.run_wait(["failed"], [])
        self.assertIn("YouTube could not process the video copia123456 (failed)", result.error)
        self.assertFalse(result.interrupted)
        self.assertEqual(recognized, [])


class StepStatusTest(unittest.TestCase):
    def status(self, phase, percent, detail=None):
        state = {**STATE, "phase": phase, "paused": False, "step_percent": percent, "step_detail": detail}
        return recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0))

    def test_a_download_shows_its_percent_and_speed(self):
        self.assertEqual(self.status("download", 68, "4.92MiB/s"), "Downloading 68% · 4.92MiB/s")

    def test_each_step_has_its_own_label(self):
        self.assertEqual(self.status("convert", 40), "Converting 40%")
        self.assertEqual(self.status("upload", 40), "Uploading 40%")
        self.assertEqual(self.status("audio", 70), "Preparing the audio 70%")

    def test_a_step_without_a_known_percent_shows_only_its_label(self):
        self.assertEqual(self.status("download", None), "Downloading")
        self.assertEqual(self.status("upload", None, "resuming"), "Uploading · resuming")

    def test_the_bar_follows_the_step_instead_of_the_samples(self):
        state = {**STATE, "phase": "convert", "step_percent": 40, "step_detail": None}
        self.assertEqual(recognize_tui.status_fields(state, 102.0)["percent"], 40)
        state["step_percent"] = None
        self.assertEqual(recognize_tui.status_fields(state, 102.0)["percent"], 0)

    def test_a_spanish_system_names_the_steps_in_spanish(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(self.status("download", 68), "Descargando 68%")
            self.assertEqual(self.status("convert", 1), "Convirtiendo 1%")
            self.assertEqual(self.status("upload", 2), "Subiendo 2%")
            self.assertEqual(self.status("audio", 3), "Preparando el audio 3%")


class TuiStepsTest(IsolatedCache, unittest.TestCase):
    ORIGINAL = "orig1234567"
    COPY = "copia123456"

    def run_steps_app(self, script, outcome, waiting_after=False, inspect=None, keys=(), hold=None, during=None, job=None, after_keys=(), ignore_stop=False):
        from textual.app import App

        release = threading.Event()
        self.checked = []
        self.seen = {}
        recognized = []
        self.clipboard = []
        job = job or upload.Job(self.ORIGINAL, "Set", None, None, True, None)
        self.needs_wait_args = []
        self.claim_owns = []
        self.steps_calls = 0

        def fake_steps(given, progress, stop):
            self.steps_calls += 1
            self.seen["job"] = given
            self.seen["stop"] = stop
            for event in script:
                progress(event)
            if ignore_stop:
                stop.wait(5)
                return outcome
            if hold:
                self.seen["stopped"] = stop.wait(5)
                time.sleep(0.2)
                self.seen["thread_done"] = True
                raise upload.Cancelled()
            if inspect:
                release.wait(5)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def details(client, video_id):
            self.checked.append(video_id)
            return "succeeded", time.time()

        async def recognition(video_id, gaps, progress, resume=None):
            recognized.append(video_id)
            return []

        def drive(app, *args, **kwargs):
            original_exit = app.exit

            def exit_noting_the_thread(*args, **kwargs):
                self.seen.setdefault("done_at_exit", self.seen.get("thread_done", False))
                return original_exit(*args, **kwargs)

            app.exit = exit_noting_the_thread

            async def scenario():
                async with app.run_test() as pilot:
                    if during:
                        for _ in range(50):
                            await pilot.pause(0.05)
                            if hold in str(app.query_one("#status-text").render()):
                                break
                        await during(app, pilot)
                        return
                    if inspect:
                        for _ in range(50):
                            await pilot.pause(0.05)
                            if "Downloading" in str(app.query_one("#status-text").render()):
                                break
                        self.seen["during"] = inspect(app)
                        for key in keys:
                            await pilot.press(key)
                        await pilot.pause()
                        self.seen["busy_log"] = [line.text.strip() for line in app.query_one("#log").lines]
                        release.set()
                    await app.workers.wait_for_complete()
                    self.seen["log"] = [line.text.strip() for line in app.query_one("#log").lines]
                    for key in after_keys:
                        await pilot.press(key)
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(upload, "run_steps", fake_steps), \
                mock.patch.object(tracklist.subprocess, "run", lambda command, **kwargs: self.clipboard.append(kwargs["input"].decode())), \
                mock.patch.object(upload, "needs_wait", lambda video_id: self.needs_wait_args.append(video_id) or waiting_after), \
                mock.patch.object(upload, "youtube_client", lambda: "client"), \
                mock.patch.object(upload, "processing_details", details), \
                mock.patch.object(upload, "CLAIMS_POLL", 0), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch.object(tracklist, "probe_duration", lambda video_id: 600), \
                mock.patch.object(tracklist, "try_claims", lambda video_id, own: self.claim_owns.append(own) or ({}, TuiWaitTest.CLAIMS[:1], None)), \
                mock.patch.object(tracklist, "recognize_gaps_async", recognition), \
                mock.patch.object(tracklist, "notify_ready", lambda title: None), \
                mock.patch.object(App, "run", drive):
            result = recognize_tui.run_app(job.original_id, None, False, True, job=job)
        return result, recognized

    def test_the_status_and_the_bar_follow_the_running_step(self):
        def inspect(app):
            return str(app.query_one("#status-text").render()), app.query_one("#bar").progress

        self.run_steps_app(
            [("step", "download", 68.0, "4.92MiB/s")],
            upload.Outcome(self.COPY, None, True, 600), inspect=inspect,
        )
        text, bar = self.seen["during"]
        self.assertIn("Downloading 68% · 4.92MiB/s", text)
        self.assertEqual(bar, 68)

    def test_the_log_marks_each_new_step_once_the_upload_and_the_warnings(self):
        script = [
            ("step", "download", 10.0, None), ("step", "download", 90.0, None),
            ("step", "convert", 50.0, None), ("step", "upload", 20.0, None),
            ("step-done", "upload", self.COPY), ("warning", "careful now"),
        ]
        self.run_steps_app(script, upload.Outcome(self.COPY, None, True, 600))
        log = self.seen["log"]
        self.assertEqual(log.count("Downloading"), 1)
        self.assertLess(log.index("Downloading"), log.index("Converting"))
        self.assertLess(log.index("Converting"), log.index("Uploading"))
        self.assertIn(f"Uploaded as https://youtu.be/{self.COPY}", log)
        self.assertIn("careful now", log)

    def test_the_copied_link_of_a_local_file_is_the_uploaded_copy_since_it_has_no_original(self):
        source = Path("/tmp/set.m4a")
        job = upload.Job(None, None, source, None, True, source)
        self.run_steps_app([], upload.Outcome(self.COPY, None, True, 600), job=job, after_keys=("c",))
        self.assertIn(f"https://www.youtube.com/watch?v={self.COPY}", self.clipboard[0])

    def test_the_copied_tracklist_of_a_local_file_is_headed_by_its_title(self):
        source = Path("/tmp/set.m4a")
        job = upload.Job(None, "set", source, None, True, source)
        self.run_steps_app([], upload.Outcome(self.COPY, None, True, 600), job=job, after_keys=("c",))
        self.assertEqual(self.clipboard[0].splitlines()[0], "set")

    def test_the_steps_get_the_job_and_a_stop_event(self):
        self.run_steps_app([], upload.Outcome(self.COPY, None, True, 600))
        self.assertEqual(self.seen["job"].original_id, self.ORIGINAL)
        self.assertIsInstance(self.seen["stop"], threading.Event)

    def test_after_an_upload_the_result_is_the_copy_and_the_wait_starts_on_it(self):
        result, recognized = self.run_steps_app(
            [("step-done", "upload", self.COPY)],
            upload.Outcome(self.COPY, None, True, 600), waiting_after=True,
        )
        self.assertEqual(result.video_id, self.COPY)
        self.assertEqual(self.checked, [self.COPY])
        self.assertEqual(recognized, [self.COPY])
        self.assertEqual(result.duration, 600)
        self.assertFalse(result.interrupted)

    def test_a_quota_fallback_goes_on_with_shazam_on_the_original_without_waiting(self):
        result, recognized = self.run_steps_app(
            [("warning", "quota used up")],
            upload.Outcome(self.ORIGINAL, None, False, None),
        )
        self.assertEqual(result.video_id, self.ORIGINAL)
        self.assertEqual(self.checked, [])
        self.assertEqual(self.needs_wait_args, [self.ORIGINAL])
        self.assertEqual(self.claim_owns, [False])
        self.assertIn("quota used up", self.seen["log"])
        self.assertEqual(recognized, [self.ORIGINAL])
        self.assertIsNone(result.error)

    def test_restart_and_new_video_are_ignored_while_the_steps_run_so_nothing_uploads_twice(self):
        def inspect(app):
            return None

        for key in ("r", "n", "x"):
            self.run_steps_app(
                [("step", "download", 68.0, None)],
                upload.Outcome(self.COPY, None, True, 600), inspect=inspect, keys=(key,),
            )
            self.assertEqual(self.steps_calls, 1)
            self.assertEqual(self.seen["busy_log"].count("[!] wait until the download, conversion and upload finish"), 1)

    def test_a_spanish_system_explains_why_the_keys_are_ignored(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(messages.t("log_busy_steps"), "[!] espera a que terminen la descarga, la conversion y la subida")

    def logged(self, app):
        return [line.text.strip() for line in app.query_one("#log").lines]

    def test_quitting_during_the_download_stops_it_and_waits_for_it_before_closing(self):
        async def quit_app(app, pilot):
            await pilot.press("q")
            await pilot.pause()
            self.seen["log"] = self.logged(app)
            await app.workers.wait_for_complete()

        result, recognized = self.run_steps_app(
            [("step", "download", 30.0, None)], None, hold="Downloading", during=quit_app,
        )
        self.assertTrue(self.seen["stopped"])
        self.assertTrue(self.seen["thread_done"])
        self.assertTrue(self.seen["done_at_exit"])
        self.assertIn("[*] stopping...", self.seen["log"])
        self.assertTrue(result.interrupted)
        self.assertIsNone(result.error)
        self.assertEqual(recognized, [])
        self.assertEqual(self.checked, [])

    def test_a_stop_that_lands_as_the_steps_finish_counts_as_leaving_early(self):
        async def quit_app(app, pilot):
            await pilot.press("q")
            await pilot.pause()
            await app.workers.wait_for_complete()

        result, recognized = self.run_steps_app(
            [("step", "download", 30.0, None)], upload.Outcome(self.COPY, None, True, 600),
            hold="Downloading", during=quit_app, ignore_stop=True,
        )
        self.assertTrue(result.interrupted)
        self.assertEqual(recognized, [])
        self.assertEqual(self.checked, [])

    def test_quitting_during_the_upload_asks_first_because_it_would_upload_again(self):
        async def ask(app, pilot):
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.seen["asked"] = self.logged(app)
            self.seen["stop_after_asking"] = self.seen["stop"].is_set()
            await pilot.press("escape")
            await pilot.pause()
            self.seen["stop_after_escape"] = self.seen["stop"].is_set()
            self.seen["after_escape"] = self.logged(app)
            await pilot.press("q")
            await pilot.press("y")
            await pilot.pause()
            await app.workers.wait_for_complete()

        result, _ = self.run_steps_app(
            [("step", "upload", 20.0, None)], None, hold="Uploading", during=ask,
        )
        question = "[!] the upload is half done: if you quit, it uploads again next time and uses quota. Press y to quit"
        self.assertEqual(self.seen["asked"].count(question), 1)
        self.assertFalse(self.seen["stop_after_asking"])
        self.assertFalse(self.seen["stop_after_escape"])
        self.assertIn("[*] cancelled", self.seen["after_escape"])
        self.assertTrue(self.seen["stopped"])
        self.assertTrue(self.seen["thread_done"])
        self.assertTrue(result.interrupted)

    def test_quitting_once_the_upload_is_done_stops_without_asking_even_though_the_status_still_says_uploading(self):
        async def quit_app(app, pilot):
            await pilot.press("q")
            await pilot.pause()
            self.seen["log"] = self.logged(app)
            self.seen["stop_set"] = self.seen["stop"].is_set()
            await app.workers.wait_for_complete()

        result, _ = self.run_steps_app(
            [("step", "upload", 90.0, None), ("step-done", "upload", self.COPY)],
            None, hold="Uploading", during=quit_app,
        )
        self.assertTrue(self.seen["stop_set"])
        self.assertIn("[*] stopping...", self.seen["log"])
        self.assertTrue(result.interrupted)

    def test_a_quit_question_is_dropped_when_the_steps_end_so_the_other_keys_work_again(self):
        async def ask_then_end(app, pilot):
            await pilot.press("q")
            await pilot.pause()
            self.seen["pending_while_asking"] = app.pending
            app.stop.set()
            await app.workers.wait_for_complete()
            self.seen["pending_after"] = app.pending

        self.run_steps_app(
            [("step", "upload", 20.0, None)], None, hold="Uploading", during=ask_then_end,
        )
        self.assertEqual(self.seen["pending_while_asking"], "quit")
        self.assertIsNone(self.seen["pending_after"])

    def test_a_spanish_system_asks_before_abandoning_the_upload_and_says_it_is_stopping(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(messages.t("log_stopping"), "[*] parando...")
            self.assertEqual(
                messages.t("log_quit_upload", yes="s"),
                "[!] la subida esta a medias: si sales, la proxima vez se subira de nuevo y gastara cuota. Pulsa s para salir",
            )

    def test_quitting_after_the_steps_finished_is_not_an_interruption(self):
        result, _ = self.run_steps_app([], upload.Outcome(self.COPY, None, True, 600))
        self.assertFalse(result.interrupted)
        self.assertTrue(result.steps_done)

    def test_quitting_before_the_steps_finish_says_they_are_not_done(self):
        async def quit_app(app, pilot):
            await pilot.press("q")
            await pilot.pause()
            await app.workers.wait_for_complete()

        result, _ = self.run_steps_app(
            [("step", "download", 30.0, None)], None, hold="Downloading", during=quit_app,
        )
        self.assertFalse(result.steps_done)

    def test_a_local_file_with_no_quota_closes_the_app_with_the_message_for_main_to_print(self):
        result, recognized = self.run_steps_app([], SystemExit("quota used up"))
        self.assertEqual(result.error, "quota used up")
        self.assertEqual(recognized, [])


class NotifyReadyTest(unittest.TestCase):
    def test_it_shows_a_macos_notification_with_the_title_escaped(self):
        calls = []
        tracklist.notify_ready('Set "Live" \\ 8', run=lambda argv, **kwargs: calls.append((argv, kwargs)))
        argv, kwargs = calls[0]
        self.assertEqual(argv, [
            "osascript", "-e",
            'display notification "Tracklist ready: Set \\"Live\\" \\\\ 8" with title "tracklist"',
        ])
        self.assertFalse(kwargs["check"])

    def test_a_missing_osascript_is_ignored(self):
        def missing(argv, **kwargs):
            raise OSError("no osascript")

        tracklist.notify_ready("Set", run=missing)


def tui_result(**changes):
    fields = dict(found=[], entries=[], gaps=[], duration=600, title=None, error=None,
                  interrupted=False, video_id="copia123456", steps_done=True)
    return recognize_tui.Result(**{**fields, **changes})


class TuiWaitInMainTest(IsolatedUploads, IsolatedCache, unittest.TestCase):
    def run_main(self, outcome, waited=True, textual=True):
        upload.remember("youtube:tZ6YRoNjvPI", "copia123456")
        self.calls = {"run_app": [], "render": [], "deleted": [], "terminal_wait": [], "jobs": [], "steps": [], "shown": []}

        def fake_app(video_id, duration, label, own=False, waiting=False, job=None, shown_id=None):
            self.calls["run_app"].append(waiting)
            self.calls["shown"].append(shown_id)
            self.calls["jobs"].append(job)
            return outcome

        errors = io.StringIO()
        with mock.patch.object(tracklist, "missing_tools", lambda: []), \
                fake_job("copia123456", "Set", True), \
                mock.patch.object(upload, "run_steps", lambda *args: self.calls["steps"].append(args) or upload.Outcome("copia123456", None, True, None)), \
                mock.patch.object(upload, "wait_until_ready", lambda video_id, report: self.calls["terminal_wait"].append(video_id) or waited), \
                mock.patch.object(upload, "offer_delete", self.calls["deleted"].append), \
                mock.patch.object(tracklist, "render", lambda *args: self.calls["render"].append(args) or ""), \
                mock.patch.object(tracklist, "recognition_available", lambda: True), \
                mock.patch.object(recognize_ui, "textual_available", lambda: textual), \
                mock.patch.object(recognize_tui, "run_app", fake_app), \
                mock.patch("sys.argv", ["tracklist", "https://www.youtube.com/watch?v=tZ6YRoNjvPI", "--duration", "600"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            tracklist.main()
        return errors.getvalue()

    def test_the_tui_does_the_wait_instead_of_the_terminal(self):
        self.run_main(tui_result())
        self.assertEqual(self.calls["run_app"], [True])
        self.assertEqual(self.calls["terminal_wait"], [])

    def test_the_tui_is_told_the_original_so_the_copied_link_is_not_the_upload(self):
        self.run_main(tui_result())
        self.assertEqual(self.calls["shown"], ["tZ6YRoNjvPI"])

    def test_the_tui_gets_the_job_so_its_steps_are_shown_instead_of_run_in_a_blank_terminal(self):
        self.run_main(tui_result())
        self.assertEqual(self.calls["jobs"][0].original_id, "orig1234567")
        self.assertEqual(self.calls["steps"], [])

    def test_the_copy_the_tui_ended_with_is_the_one_offered_for_deletion_while_the_original_is_shown(self):
        self.run_main(tui_result(video_id="otracopia12"))
        self.assertEqual(self.calls["deleted"], ["otracopia12"])
        self.assertEqual(self.calls["render"][0][2], "tZ6YRoNjvPI")

    def test_the_terminal_wait_notifies_only_when_it_actually_waited(self):
        for waited, expected in ((True, ["Set"]), (False, [])):
            notified = []
            with mock.patch.object(tracklist, "notify_ready", notified.append), \
                    mock.patch.object(upload, "needs_wait", lambda video_id: True):
                self.run_main(tui_result(), waited=waited, textual=False)
            self.assertEqual(notified, expected)

    def test_quitting_the_tui_during_the_wait_prints_no_tracklist_and_offers_no_delete(self):
        errors = self.run_main(tui_result(interrupted=True))
        self.assertIn("run 'tracklist https://www.youtube.com/watch?v=tZ6YRoNjvPI' again", errors)
        self.assertEqual(self.calls["render"], [])
        self.assertEqual(self.calls["deleted"], [])

    def test_quitting_the_tui_during_the_steps_says_to_run_again_to_continue_not_that_content_id_is_working(self):
        errors = self.run_main(tui_result(interrupted=True, steps_done=False, found=[], video_id="orig1234567"))
        self.assertIn("Stopped before finishing: run 'tracklist https://www.youtube.com/watch?v=tZ6YRoNjvPI' again to continue", errors)
        self.assertNotIn("Content ID is still working", errors)
        self.assertEqual(self.calls["render"], [])
        self.assertEqual(self.calls["deleted"], [])

    def test_a_spanish_system_says_how_to_resume_after_stopping_the_steps(self):
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.assertEqual(
                messages.t("resume_steps", command="x"),
                "Parado antes de terminar: vuelve a lanzar 'tracklist x' para continuar",
            )


class ContentIdStatusTest(unittest.TestCase):
    def test_the_wait_shows_how_many_tracks_content_id_found_and_since_when(self):
        state = {**STATE, "phase": "content_id", "paused": False, "claims": 5, "uploaded_at": 1000.0}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 102.0, 1720.0)),
            "Content ID: 5 tracks, 12:00 since upload",
        )

    def test_the_time_since_upload_keeps_ticking_between_claims_polls(self):
        state = {**STATE, "phase": "content_id", "paused": False, "claims": 5, "uploaded_at": 1000.0}
        self.assertEqual(
            recognize_tui.status_text(state, recognize_tui.status_fields(state, 103.0, 1721.0)),
            "Content ID: 5 tracks, 12:01 since upload",
        )

    def test_processing_on_youtube_has_its_own_label(self):
        self.assertEqual(recognize_tui.phase_label("youtube"), "YouTube is processing the video")


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

    def test_enter_with_nothing_new_in_downloads_asks_for_the_file_instead_of_blaming_one(self):
        fresh = self.write_secret(self.root / "fresh.json")
        self.run_wizard([""] * 7 + [str(fresh)])
        self.assertIn("No new client_secret*.json found in ~/Downloads: download it or paste the JSON's path.", self.reported)
        self.assertFalse(any(m.startswith("That file") for m in self.reported))

    def test_a_spanish_system_asks_for_the_missing_file_in_spanish(self):
        fresh = self.write_secret(self.root / "fresh.json")
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.run_wizard([""] * 7 + [str(fresh)])
        self.assertIn("No encuentro un client_secret*.json nuevo en ~/Downloads: descargalo o pega la ruta del JSON.", self.reported)

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
        self.assertTrue(any("App de escritorio (Desktop app)" in m for m in self.reported))
        self.assertTrue(any(m.startswith("Paso 1/5") for m in self.reported))

    def step_text(self, number, language):
        good = self.write_secret(self.root / "client_secret.json")
        with mock.patch.object(messages, "LANGUAGE", language):
            self.run_wizard([""] * 6 + [str(good)])
        word = "Step" if language == "en" else "Paso"
        return next(m for m in self.reported if m.startswith(f"{word} {number}/5"))

    def test_the_app_setup_step_lists_each_google_screen_so_nobody_has_to_guess_what_comes_next(self):
        text = self.step_text(3, "en")
        for button in ("Get started", "External", "Next", "Create"):
            self.assertIn(button, text)
        self.assertGreaterEqual(text.count("\n    - "), 4)

    def test_every_step_names_the_exact_button_to_press(self):
        expected = {1: "Create", 2: "Enable", 4: "Publish app", 5: "Download JSON"}
        for number, button in expected.items():
            self.reported.clear()
            self.assertIn(button, self.step_text(number, "en"))

    def test_spanish_steps_pair_each_button_with_the_english_label_the_console_may_show(self):
        text = self.step_text(5, "es")
        self.assertIn("App de escritorio (Desktop app)", text)
        self.assertIn("Descargar JSON (Download JSON)", text)

    def test_the_wizard_warns_up_front_how_many_pages_open_and_that_the_unverified_warning_is_normal(self):
        good = self.write_secret(self.root / "client_secret.json")
        self.run_wizard([""] * 6 + [str(good)])
        intro = self.reported[0]
        self.assertIn("5 Google pages", intro)
        self.assertIn("hasn't verified this app", intro)

    def test_the_spanish_intro_gives_the_same_up_front_warning(self):
        good = self.write_secret(self.root / "client_secret.json")
        with mock.patch.object(messages, "LANGUAGE", "es"):
            self.run_wizard([""] * 6 + [str(good)])
        intro = self.reported[0]
        self.assertIn("5 paginas de Google", intro)
        self.assertIn("no ha verificado esta aplicacion", intro)


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


class TuiLinkTest(unittest.TestCase):
    URL = "https://youtu.be/JQ4i5zJu0M8"

    def test_a_url_in_a_log_line_carries_a_terminal_link(self):
        text = recognize_tui.linked(f"Uploaded as {self.URL}")
        self.assertEqual(text.plain, f"Uploaded as {self.URL}")
        links = [(text.plain[span.start:span.end], span.style.link) for span in text.spans]
        self.assertEqual(links, [(self.URL, self.URL)])

    def test_a_line_without_urls_keeps_its_markup(self):
        self.assertEqual(recognize_tui.linked("[*] paused").plain, "[*] paused")

    def test_clicking_the_uploaded_link_in_the_log_opens_it_in_the_browser(self):
        from textual.app import App
        from textual.widgets import RichLog

        opened = []

        async def no_recognition(video_id, gaps, progress, resume=None):
            return []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test(size=(160, 40)) as pilot:
                    await app.workers.wait_for_complete()
                    app.open_url = lambda url, **options: opened.append(url)
                    log = app.query_one("#log", RichLog)
                    log.clear()
                    app.log_line(messages.t("uploaded", video_id="JQ4i5zJu0M8"))
                    offset = (len("Uploaded as ") + 5, 1)
                    settled = 0
                    for _ in range(300):
                        await pilot.pause(0.01)
                        point = (log.region.x + offset[0], log.region.y + offset[1])
                        settled = settled + 1 if app.screen.get_style_at(*point).link else 0
                        if settled == 30:
                            break
                    await pilot.click("#log", offset=offset)
                    await pilot.pause()
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(tracklist, "try_claims", lambda video_id, own: ({}, TuiWaitTest.CLAIMS[:1], None)), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch.object(App, "run", drive):
            recognize_tui.run_app("copia123456", 600, False, True)
        self.assertEqual(opened, [self.URL])


class TuiCopyLinkTest(unittest.TestCase):
    ORIGINAL = "orig1234567"
    COPY = "copia123456"
    TYPED = "typed123456"

    def copied(self, presses, shown_id=None):
        from textual.app import App

        clipboard = []

        async def no_recognition(video_id, gaps, progress, resume=None):
            return []

        def drive(app, *args, **kwargs):
            async def scenario():
                async with app.run_test() as pilot:
                    await app.workers.wait_for_complete()
                    for keys in presses:
                        await pilot.press(*keys)
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                    await pilot.press("c")
                    await pilot.press("q")
            asyncio.run(scenario())

        with mock.patch.object(tracklist, "try_claims", lambda video_id, own: ({}, TuiWaitTest.CLAIMS[:1], None)), \
                mock.patch.object(tracklist, "recognize_gaps_async", no_recognition), \
                mock.patch.object(tracklist, "fetch_videos", lambda ids, session: {}), \
                mock.patch.object(tracklist.subprocess, "run", lambda command, **kwargs: clipboard.append(kwargs["input"].decode())), \
                mock.patch.object(App, "run", drive):
            recognize_tui.run_app(self.COPY, 600, False, True, shown_id=shown_id)
        return clipboard[0]

    def test_the_copied_tracklist_links_the_original_video_not_the_uploaded_copy(self):
        text = self.copied([], shown_id=self.ORIGINAL)
        self.assertIn(f"https://www.youtube.com/watch?v={self.ORIGINAL}", text)
        self.assertNotIn(self.COPY, text)

    def test_the_copied_tracklist_links_the_video_typed_with_n_instead_of_the_original(self):
        typed = f"https://www.youtube.com/watch?v={self.TYPED}"
        text = self.copied([["n"], list(typed) + ["enter"]], shown_id=self.ORIGINAL)
        self.assertIn(typed, text)
        self.assertNotIn(self.ORIGINAL, text)

    def test_without_an_original_the_copied_link_is_the_video_the_app_was_given(self):
        text = self.copied([])
        self.assertIn(f"https://www.youtube.com/watch?v={self.COPY}", text)
