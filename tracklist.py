#!/usr/bin/env python3
import argparse
import asyncio
import bisect
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from messages import t

BASE = "https://studio.youtube.com/youtubei/v1/creator"
ORIGIN = "https://studio.youtube.com"
STUDIO_CLIENT_VERSION = "1.20260805.02.02"
CLAIMS_ENDPOINT = f"{BASE}/list_creator_received_claims?alt=json"
VIDEOS_ENDPOINT = f"{BASE}/get_creator_videos?alt=json"
CONFIG_FILE = Path.home() / ".config" / "tracklist" / "config.json"
SYSTEM_TOOLS = ["yt-dlp", "ffmpeg", "ffprobe"]
BROWSERS = {
    "chrome": "Google Chrome.app",
    "brave": "Brave Browser.app",
    "edge": "Microsoft Edge.app",
    "firefox": "Firefox.app",
    "safari": "Safari.app",
    "vivaldi": "Vivaldi.app",
    "opera": "Opera.app",
    "chromium": "Chromium.app",
}
APP_DIRS = [Path("/Applications"), Path.home() / "Applications"]
CACHE_DIR = Path.home() / ".config" / "tracklist" / "cache"
AUDIO_DIR = Path.home() / "Library" / "Caches" / "tracklist"
MIN_GAP_SECONDS = 90
SAMPLE_SECONDS = 10
SAMPLE_INTERVAL = 90
SAMPLE_LEAD = 15
MERGE_WINDOW_SECONDS = 180
START_WORKERS = 3
MAX_WORKERS = 3
RECOVER_AFTER = 6
SAMPLE_PACING = 0.5
BACKOFF_START = 3
BACKOFF_CEILING = 30
BACKOFF_RETRIES = 4


def cache_path(video_id):
    return CACHE_DIR / f"{video_id}.json"


def audio_path(video_id):
    return AUDIO_DIR / f"{video_id}.mp3"


def load_cache(video_id):
    empty = {"samples": {}, "title": None, "duration": None}
    path = cache_path(video_id)
    if not path.exists():
        return empty
    try:
        stored = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return empty
    return {
        "samples": {int(offset): match for offset, match in stored.get("samples", {}).items()},
        "title": stored.get("title"),
        "duration": stored.get("duration"),
    }


def write_cache(video_id, cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(video_id).write_text(json.dumps(cache))


def save_sample(video_id, offset, match):
    cache = load_cache(video_id)
    cache["samples"][offset] = match
    write_cache(video_id, cache)


def save_video_details(video_id, title, duration):
    cache = load_cache(video_id)
    cache["title"] = title
    cache["duration"] = duration
    write_cache(video_id, cache)


def clear_cache(video_id):
    cache_path(video_id).unlink(missing_ok=True)
    audio_path(video_id).unlink(missing_ok=True)


def build_authorization(cookies, timestamp):
    def digest(value):
        return hashlib.sha1(f"{timestamp} {value} {ORIGIN}".encode()).hexdigest()

    return " ".join(
        f"{scheme} {timestamp}_{digest(cookies[name])}"
        for scheme, name in (
            ("SAPISIDHASH", "SAPISID"),
            ("SAPISID1PHASH", "__Secure-1PAPISID"),
            ("SAPISID3PHASH", "__Secure-3PAPISID"),
        )
    )


def parse_cookie_file(path):
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar if c.domain.endswith("youtube.com")}


def browser_cookies(browser):
    with tempfile.TemporaryDirectory() as tmp:
        jar_path = Path(tmp) / "cookies.txt"
        command = [
            "yt-dlp", "--quiet", "--no-warnings", "--ignore-errors", "--skip-download",
            "--cookies-from-browser", browser, "--cookies", str(jar_path),
            "https://www.youtube.com/robots.txt",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit(t("ytdlp_missing"))
        if not jar_path.exists():
            if browser == "safari" and "Operation not permitted" in result.stderr:
                sys.exit(t("safari_disk_access"))
            sys.exit(t("cookies_failed", browser=browser, detail=result.stderr.strip()[-300:]))
        return parse_cookie_file(jar_path)


def installed_browsers(app_dirs):
    return [
        name for name, app in BROWSERS.items()
        if any((directory / app).exists() for directory in app_dirs)
    ]


def pick_browser(candidates, read_cookies):
    for browser in candidates:
        try:
            cookies = read_cookies(browser)
        except SystemExit:
            continue
        if "SAPISID" in cookies:
            return browser
    return None


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_browser(browser):
    config = load_config()
    config["browser"] = browser
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config))


def current_browser():
    saved = load_config().get("browser")
    if saved:
        return saved
    browser = pick_browser(installed_browsers(APP_DIRS), browser_cookies)
    if browser is None:
        sys.exit(t("no_browser_session"))
    save_browser(browser)
    return browser


def studio_session(cookies, timestamp):
    return {
        "headers": {
            "cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
            "authorization": build_authorization(cookies, timestamp),
            "origin": ORIGIN,
            "x-origin": ORIGIN,
            "x-youtube-client-name": "62",
            "x-youtube-client-version": STUDIO_CLIENT_VERSION,
        },
        "context": {"client": {"clientName": 62, "clientVersion": STUDIO_CLIENT_VERSION}},
    }


def fresh_session():
    browser = current_browser()
    cookies = browser_cookies(browser)
    if "SAPISID" not in cookies:
        sys.exit(t("browser_signed_out", browser=browser))
    return studio_session(cookies, int(time.time()))


def post(endpoint, payload, session):
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={**session["headers"], "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            sys.exit(t("session_rejected"))
        if error.code == 403:
            sys.exit(t("studio_other_account"))
        if error.code == 404:
            sys.exit(t("studio_404", version=STUDIO_CLIENT_VERSION))
        sys.exit(t("http_error", code=error.code, body=error.read()[:300].decode(errors="replace")))


def fetch_claims(video_id, session):
    return post(
        CLAIMS_ENDPOINT,
        {
            "context": session["context"],
            "videoId": video_id,
            "criticalRead": False,
            "includeLicensingOptions": False,
            "includeCommunicationEmail": False,
            "isCreatorMusicV2": False,
        },
        session,
    )


def fetch_videos(video_ids, session):
    response = post(
        VIDEOS_ENDPOINT,
        {
            "context": session["context"],
            "failOnError": False,
            "videoIds": video_ids,
            "mask": {"videoId": True, "title": True, "lengthSeconds": True},
            "criticalRead": False,
        },
        session,
    )
    return {
        video["videoId"]: {
            "title": video.get("title", ""),
            "length": int(video.get("lengthSeconds") or 0),
        }
        for video in response.get("videos", [])
    }


def describe(claim):
    asset = claim.get("asset", {})
    meta = asset.get("metadata", {})
    recording = meta.get("soundRecording") or asset.get("srMetadata") or {}
    if not recording.get("title"):
        composition = meta.get("composition", {})
        recording = {
            "title": composition.get("title", "?"),
            "artists": composition.get("writers", []),
        }
    return {
        "title": recording.get("title", "?"),
        "artist": ", ".join(recording.get("artists", [])) or "?",
        "label": recording.get("recordLabel", ""),
    }


def extract(response):
    entries = []
    for claim in response.get("receivedClaims", []):
        match = claim.get("matchDetails", {})
        start = match.get("longestMatchStartTimeSeconds")
        if start is None:
            continue
        entry = describe(claim)
        entry["start"] = int(start)
        entry["duration"] = int(match.get("longestMatchDurationSeconds", 0))
        entries.append(entry)
    return sorted(entries, key=lambda entry: entry["start"])


def find_gaps(entries, total=None):
    gaps = []
    cursor = 0
    for entry in entries:
        if entry["start"] - cursor >= MIN_GAP_SECONDS:
            gaps.append((cursor, entry["start"]))
        cursor = max(cursor, entry["start"] + entry["duration"])
    if total and total - cursor >= MIN_GAP_SECONDS:
        gaps.append((cursor, total))
    return gaps


def try_claims(video_id, own=False):
    try:
        session = fresh_session()
        entries = extract(fetch_claims(video_id, session))
    except SystemExit as error:
        stale = error.code == t("studio_404", version=STUDIO_CLIENT_VERSION)
        return None, [], error.code if own or stale else None
    return session, entries, None


def scan_gaps(duration):
    if not duration or duration < SAMPLE_LEAD + SAMPLE_SECONDS:
        return []
    return [(0, duration)]


def plan_samples(start, end):
    return list(range(start + SAMPLE_LEAD, end - SAMPLE_SECONDS + 1, SAMPLE_INTERVAL))


def backoff_delay(failures):
    return min(BACKOFF_CEILING, BACKOFF_START * 2 ** failures)


class Limiter:
    def __init__(self):
        self.workers = START_WORKERS
        self.clean = 0

    def throttle(self):
        self.workers = max(1, self.workers // 2)
        self.clean = 0

    def reward(self):
        self.clean += 1
        if self.clean >= RECOVER_AFTER:
            self.workers = min(MAX_WORKERS, self.workers + 1)
            self.clean = 0


class Gate:
    def __init__(self, limiter):
        self.limiter = limiter
        self.active = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def acquire(self):
        while self.active >= self.limiter.workers:
            self.idle.clear()
            await self.idle.wait()
        self.active += 1

    def release(self):
        self.active -= 1
        self.idle.set()


def base_title(title):
    stripped = re.split(r"\s*[(\[]|\s+-\s+", title.strip(), maxsplit=1)[0].strip()
    return (stripped or title.strip()).lower()


def same_track(one, other):
    return (
        one["artist"].strip().lower() == other["artist"].strip().lower()
        and base_title(one["title"]) == base_title(other["title"])
    )


def overlaps(entry, suggestion):
    return (
        suggestion["start"] < entry["start"] + entry["duration"]
        and entry["start"] < suggestion["start"] + suggestion["duration"]
    )


def absorbs(entry, suggestion):
    if not same_track(entry, suggestion):
        return False
    return (
        within_merge_window(entry, suggestion["start"])
        and within_merge_window(suggestion, entry["start"])
    )


def stretch(entry, suggestion):
    end = max(entry["start"] + entry["duration"], suggestion["start"] + suggestion["duration"])
    entry["start"] = min(entry["start"], suggestion["start"])
    entry["duration"] = end - entry["start"]


def drop_claimed(claimed, suggestions):
    kept = []
    for suggestion in suggestions:
        absorbed = next((entry for entry in claimed if absorbs(entry, suggestion)), None)
        if absorbed:
            stretch(absorbed, suggestion)
            continue
        if any(overlaps(entry, suggestion) for entry in claimed):
            continue
        kept.append(suggestion)
    return kept


def within_merge_window(entry, start):
    return start - (entry["start"] + entry["duration"]) <= MERGE_WINDOW_SECONDS


def merge_samples(samples):
    suggestions = []
    previous = None
    for offset, match in samples:
        if match is None:
            continue
        if previous and same_track(previous, match) and within_merge_window(previous, offset):
            previous["duration"] = offset + SAMPLE_SECONDS - previous["start"]
            continue
        previous = {**match, "label": "", "start": offset, "duration": SAMPLE_SECONDS, "suggested": True}
        suggestions.append(previous)
    return suggestions


def probe_duration(video_id):
    command = [
        "yt-dlp", "--quiet", "--no-warnings", "--skip-download",
        "--print", "duration",
        "--cookies-from-browser", current_browser(),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(float(result.stdout.strip()))
    except ValueError:
        return None


def download_audio(video_id, directory):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    target = audio_path(video_id)
    command = [
        "yt-dlp", "--quiet", "--no-warnings", "-x", "--audio-format", "mp3",
        "--cookies-from-browser", current_browser(),
        "-o", str(target.with_suffix(".%(ext)s")),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        print(t("download_audio_failed"), file=sys.stderr)
        return None
    return target


def cut_sample(source, offset, directory):
    target = directory / f"{source.stem}-{offset}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(offset), "-t", str(SAMPLE_SECONDS),
         "-i", str(source), str(target)],
        check=True, capture_output=True,
    )
    return target


def recognize_gaps_async(video_id, gaps, progress, resume=None):
    try:
        from shazamio import Shazam
        from shazamio.exceptions import FailedDecodeJson
    except ImportError:
        sys.exit(f"{t('shazam_missing')}\n{t('reinstall')}")

    async def run():
        began = time.monotonic()
        shazam = Shazam()
        suggestions = []
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            if resume is not None:
                await resume.wait()
            cached_samples = load_cache(video_id)["samples"]
            planned = [offset for start, end in gaps for offset in plan_samples(start, end)]
            reused = sum(1 for offset in planned if offset in cached_samples)
            if reused:
                progress(("cached", reused, len(planned)))
            source = audio_path(video_id)
            if source.exists():
                progress(("download", "cached"))
            else:
                progress(("download", "start"))
                source = await asyncio.to_thread(download_audio, video_id, directory)
                if source is not None:
                    progress(("download", "done"))
            if source is None:
                progress(("done", 0, int(time.monotonic() - began)))
                return []
            for index, (start, end) in enumerate(gaps, 1):
                progress(("gap", index, len(gaps), start, end))
                samples = []
                limiter = Limiter()
                gate = Gate(limiter)

                async def recognize_sample(sample, offset):
                    for failures in range(BACKOFF_RETRIES):
                        await gate.acquire()
                        try:
                            result = await shazam.recognize_song(str(sample))
                        except FailedDecodeJson:
                            progress(("backoff", backoff_delay(failures), failures + 1))
                            before = limiter.workers
                            limiter.throttle()
                            if limiter.workers != before:
                                progress(("workers", limiter.workers))
                        except Exception:
                            return None
                        else:
                            if failures:
                                progress(("resolved", offset))
                            return result
                        finally:
                            gate.release()
                        await asyncio.sleep(backoff_delay(failures))
                    return None

                async def handle(offset):
                    if offset in cached_samples:
                        bisect.insort(samples, (offset, cached_samples[offset]), key=lambda item: item[0])
                        progress(("sample", offset, cached_samples[offset]))
                        return
                    if resume is not None:
                        await resume.wait()
                    sample = await asyncio.to_thread(cut_sample, source, offset, directory)
                    result = await recognize_sample(sample, offset)
                    track = result.get("track") if result is not None else None
                    if track:
                        before = limiter.workers
                        limiter.reward()
                        if limiter.workers != before:
                            progress(("workers", limiter.workers))
                    match = None
                    if track:
                        match = {"artist": track.get("subtitle", "?"), "title": track.get("title", "?")}
                    bisect.insort(samples, (offset, match), key=lambda item: item[0])
                    if result is not None:
                        save_sample(video_id, offset, match)
                    progress(("sample", offset, match))
                    await asyncio.sleep(SAMPLE_PACING)

                await asyncio.gather(*[handle(offset) for offset in plan_samples(start, end)])
                suggestions += merge_samples(samples)
        progress(("done", len(suggestions), int(time.monotonic() - began)))
        return suggestions

    return run()


def recognize_gaps(video_id, gaps, progress):
    return asyncio.run(recognize_gaps_async(video_id, gaps, progress))


def hhmmss(seconds):
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def render(entries, gaps, video_id, show_label, title=None):
    lines = []
    if title:
        lines.append(title)
    lines += [f"https://www.youtube.com/watch?v={video_id}", ""]
    rows = [(entry["start"], entry) for entry in entries]
    rows += [(start, {"gap": end - start, "end": end}) for start, end in gaps]
    for start, item in sorted(rows, key=lambda row: row[0]):
        if "gap" in item:
            lines.append(f"  - {hhmmss(start)} {t('render_gap', end=hhmmss(item['end']))}")
            continue
        line = f"  - {hhmmss(start)} {item['artist']} - {item['title']}"
        if item.get("suggested"):
            line += " (?)"
        if show_label and item["label"]:
            line += f"  [{item['label']}]"
        lines.append(line)
    identified = sum(entry["duration"] for entry in entries)
    missing = sum(end - start for start, end in gaps)
    suggested = sum(1 for entry in entries if entry.get("suggested"))
    suggested_part = t("render_suggested", count=suggested) if suggested else ""
    lines += [
        "",
        t("render_summary", identified=len(entries) - suggested, suggested=suggested_part,
          gaps=len(gaps), missing=hhmmss(missing), total=hhmmss(identified + missing)),
    ]
    return "\n".join(lines)


def missing_tools(which=shutil.which):
    return [tool for tool in SYSTEM_TOOLS if not which(tool)]


def recognition_available():
    try:
        import shazamio  # noqa: F401
    except ImportError:
        print(f"{t('recognition_disabled')}\n{t('reinstall')}", file=sys.stderr)
        return False
    return True


def parse_video_id(text):
    match = re.search(r"(?:v=|youtu\.be/|video/)([\w-]{11})", text)
    return match.group(1) if match else text


def local_file(text):
    path = Path(text).expanduser()
    return path if path.is_file() else None


def build_parser():
    parser = argparse.ArgumentParser(description=t("description"))
    parser.add_argument("video", nargs="?", help=t("help_video"))
    parser.add_argument("--browser", choices=list(BROWSERS), help=t("help_browser"))
    parser.add_argument("--from-json", metavar=t("metavar_file"), help=t("help_from_json"))
    parser.add_argument("--duration", type=int, help=t("help_duration"))
    parser.add_argument("--label", action="store_true", help=t("help_label"))
    parser.add_argument("--fresh", action="store_true", help=t("help_fresh"))
    parser.add_argument(
        "--recognize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=t("help_recognize"),
    )
    return parser


def main():
    absent = missing_tools()
    if absent:
        sys.exit(t("tools_missing", tools=", ".join(absent)))
    parser = build_parser()
    args = parser.parse_args()

    if args.browser:
        save_browser(args.browser)
    if not args.video and sys.stdin.isatty() and sys.stderr.isatty():
        print(t("ask_video"), end="", file=sys.stderr, flush=True)
        try:
            args.video = input().strip()
        except EOFError:
            print(file=sys.stderr)
    if not args.video:
        parser.error(t("video_required"))

    import upload

    def report(message):
        print(message, file=sys.stderr)

    local = local_file(args.video)
    original_id = None
    original_title = None
    if local:
        try:
            video_id = upload.prepare(local, report)
        except upload.QuotaExceeded:
            sys.exit(t("quota_exhausted"))
        audio = local
        own = True
    else:
        original_id = parse_video_id(args.video)
        video_id, audio, original_title, own = upload.adopt(original_id, report)
    shown_id = original_id or video_id
    if args.fresh:
        clear_cache(video_id)
    if audio:
        upload.place_audio(audio, video_id)
        args.duration = args.duration or upload.local_duration(audio)

    if not args.from_json and args.recognize and recognition_available():
        import recognize_ui

        if recognize_ui.textual_available():
            import recognize_tui

            found, entries, gaps, duration, title, error = recognize_tui.run_app(
                video_id, args.duration, args.label, own
            )
            if error:
                sys.exit(error)
            entries = sorted(entries + drop_claimed(entries, found), key=lambda entry: entry["start"])
            gaps = find_gaps(entries, duration)
            print(render(entries, gaps, shown_id, args.label, original_title or title))
            upload.offer_delete(video_id)
            return

    title = None
    duration = args.duration
    if args.from_json:
        entries = extract(json.loads(Path(args.from_json).read_text()))
        if not entries:
            sys.exit(t("no_timestamped_claims"))
    else:
        session, entries, failure = try_claims(video_id, own)
        if failure:
            report(failure)
        if duration is None and session is not None:
            info = fetch_videos([video_id], session).get(video_id)
            if info:
                title = info["title"]
                duration = info["length"] or None
        if duration is None:
            duration = load_cache(video_id)["duration"] or probe_duration(video_id)
        if not entries and duration is None:
            sys.exit(t("duration_unknown"))

    gaps = find_gaps(entries, duration) if entries else scan_gaps(duration)
    if not entries and not gaps:
        sys.exit(t("too_short"))
    if args.recognize and gaps and recognition_available():
        import recognize_ui

        found = recognize_gaps(video_id, gaps, recognize_ui._pentest_progress)
        entries = sorted(entries + drop_claimed(entries, found), key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, shown_id, args.label, original_title or title)
        print(text)
        recognize_ui.offer_clipboard(text)
        upload.offer_delete(video_id)
        return
    print(render(entries, gaps, shown_id, args.label, original_title or title))
    upload.offer_delete(video_id)


if __name__ == "__main__":
    main()
