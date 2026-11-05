import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import tracklist
from messages import t

CONFIG_DIR = Path.home() / ".config" / "tracklist"
UPLOADS_FILE = CONFIG_DIR / "uploads.json"
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded")
PROCESSING_POLL = 30
CLAIMS_POLL = 120
CLAIMS_STABLE_POLLS = 3
CLAIMS_TIMEOUT = 3600
TERMINATE_GRACE = 5
UPLOAD_CHUNK = 8 * 1024 * 1024
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
LEFTOVER_SUFFIXES = (".part", ".ytdl", ".temp")
CONSOLE_STEPS = [
    ("https://console.cloud.google.com/projectcreate", "step_project"),
    ("https://console.cloud.google.com/apis/library/youtube.googleapis.com", "step_enable"),
    ("https://console.cloud.google.com/auth/branding", "step_branding"),
    ("https://console.cloud.google.com/auth/audience", "step_publish"),
    ("https://console.cloud.google.com/auth/clients/create", "step_client"),
]
CONSOLE_URLS = [url for url, _ in CONSOLE_STEPS]
DOWNLOAD_OPTIONS = ["-N", "4", "--throttled-rate", "100K", "--retries", "10",
                    "--fragment-retries", "10", "--limit-rate", "5M", "-f", "bestaudio"]


class QuotaExceeded(Exception):
    pass


class Cancelled(Exception):
    pass


def downloads_dir():
    return tracklist.AUDIO_DIR / "downloads"


def video_owner(client, video_id):
    channels = client.channels().list(part="id", mine=True).execute()["items"]
    if not channels:
        sys.exit(t("no_channel"))
    mine = channels[0]["id"]
    items = client.videos().list(part="snippet", id=video_id).execute()["items"]
    if not items:
        return False, None
    snippet = items[0]["snippet"]
    return snippet["channelId"] == mine, snippet["title"]


def downloaded_audio(video_id):
    for path in sorted(downloads_dir().glob(f"{video_id}.*")):
        if path.suffix not in LEFTOVER_SUFFIXES and ".part-Frag" not in path.name:
            return path
    return None


def reap(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def terminate_when_stopped(process, stop):
    while process.poll() is None and not stop.wait(0.1):
        pass
    if stop.is_set() and process.poll() is None:
        process.terminate()


def run_tracked(command, parse, progress, stop):
    if stop and stop.is_set():
        raise Cancelled()
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
        if stop:
            threading.Thread(target=terminate_when_stopped, args=(process, stop), daemon=True).start()
        try:
            for line in process.stdout:
                event = parse(line)
                if event is not None and progress:
                    progress(event)
                if stop and stop.is_set():
                    break
        finally:
            reap(process)
            process.stdout.close()
        if stop and stop.is_set():
            raise Cancelled()
        if process.returncode != 0:
            errors.seek(0)
            raise subprocess.CalledProcessError(process.returncode, command, stderr=errors.read())


def clean_number(text):
    try:
        return float(ANSI_ESCAPE.sub("", text).strip().rstrip("%"))
    except ValueError:
        return None


def parse_download(line):
    percent, bar, speed = line.partition("|")
    if not bar:
        return None
    return ("step", "download", clean_number(percent), ANSI_ESCAPE.sub("", speed).strip() or None)


def parse_ffmpeg(name, duration):
    def parse(line):
        key, _, value = line.strip().partition("=")
        if key != "out_time_us" or not duration or not value.isdigit():
            return None
        return ("step", name, min(100, int(value) / 1e6 / duration * 100), None)
    return parse


def download_foreign(video_id, progress=None, stop=None):
    existing = downloaded_audio(video_id)
    if existing:
        return existing
    downloads_dir().mkdir(parents=True, exist_ok=True)
    command = ["yt-dlp", "--quiet", "--no-warnings", "--progress", "--newline",
               "--progress-template", "download:%(progress._percent_str)s|%(progress._speed_str)s",
               *DOWNLOAD_OPTIONS,
               "--cookies-from-browser", tracklist.current_browser(),
               "-o", str(downloads_dir() / f"{video_id}.%(ext)s"),
               f"https://www.youtube.com/watch?v={video_id}"]
    try:
        run_tracked(command, parse_download, progress, stop)
    except subprocess.CalledProcessError:
        return None
    return downloaded_audio(video_id)


def convert_to_video(source, target, duration=None, progress=None, stop=None):
    run_tracked(
        ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:r=1",
         "-i", str(source),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-shortest", str(target)],
        parse_ffmpeg("convert", duration), progress, stop,
    )


def local_duration(source):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(source)],
        check=True, capture_output=True, text=True,
    )
    return int(float(result.stdout))


def place_audio(source, video_id, duration=None, progress=None, stop=None):
    target = tracklist.audio_path(video_id)
    if target.exists():
        return
    tracklist.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part.mp3")
    try:
        run_tracked(
            ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
             "-i", str(source), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(partial)],
            parse_ffmpeg("audio", duration), progress, stop,
        )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def claims_settled(counts, elapsed):
    if elapsed >= CLAIMS_TIMEOUT:
        return True
    recent = counts[-CLAIMS_STABLE_POLLS:]
    return len(recent) == CLAIMS_STABLE_POLLS and recent[0] > 0 and len(set(recent)) == 1


def processing_details(client, video_id):
    items = client.videos().list(part="snippet,processingDetails", id=video_id).execute().get("items")
    if not items:
        return "processing", time.time()
    item = items[0]
    status = item["processingDetails"]["processingStatus"]
    try:
        uploaded_at = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        uploaded_at = time.time()
    return status, uploaded_at


def wait_for_processing(client, video_id, report, sleep=time.sleep):
    while True:
        status, uploaded_at = processing_details(client, video_id)
        if status == "succeeded":
            return uploaded_at
        if status != "processing":
            sys.exit(t("processing_failed", video_id=video_id, status=status))
        report(t("processing", status=status))
        sleep(PROCESSING_POLL)


def claims_elapsed(uploaded_at, now):
    return max(0, int(now - uploaded_at))


def wait_for_claims(video_id, uploaded_at, report, sleep=time.sleep, clock=time.time):
    counts = []
    while True:
        _, entries, failure = tracklist.try_claims(video_id, own=True)
        if failure:
            sys.exit(t("claims_unreadable", failure=failure, video_id=video_id))
        counts.append(len(entries))
        elapsed = claims_elapsed(uploaded_at, clock())
        report(t("claims_count", count=len(entries), elapsed=tracklist.hhmmss(elapsed)))
        if elapsed >= CLAIMS_TIMEOUT:
            report(t("content_id_timeout"))
            tracklist.mark_content_id_done(video_id)
            return
        if claims_settled(counts, elapsed):
            tracklist.mark_content_id_done(video_id)
            return
        sleep(CLAIMS_POLL)


def wait_until_ready(video_id, report):
    if tracklist.load_cache(video_id)["content_id_done"]:
        return False
    uploaded_at = wait_for_processing(youtube_client(), video_id, report)
    wait_for_claims(video_id, uploaded_at, report)
    return True


def needs_wait(video_id):
    return video_id in load_uploads().values() and not tracklist.load_cache(video_id)["content_id_done"]


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_uploads():
    if not UPLOADS_FILE.exists():
        return {}
    return json.loads(UPLOADS_FILE.read_text())


def known_upload(path):
    return load_uploads().get(file_hash(path))


def remember(key, video_id):
    uploads = load_uploads()
    uploads[key] = video_id
    UPLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_FILE.write_text(json.dumps(uploads))


def remember_upload(path, video_id):
    remember(file_hash(path), video_id)


def remote_key(video_id):
    return f"youtube:{video_id}"


def forget_upload(video_id):
    for key, known in load_uploads().items():
        if known == video_id and key.startswith(remote_key("")):
            for leftover in downloads_dir().glob(f"{key.removeprefix(remote_key(''))}.*"):
                leftover.unlink()
    uploads = {digest: known for digest, known in load_uploads().items() if known != video_id}
    UPLOADS_FILE.write_text(json.dumps(uploads))


def print_stderr(message):
    print(message, file=sys.stderr)


def valid_client_secret(path):
    try:
        return bool(json.loads(Path(path).read_text())["installed"]["client_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def find_client_secret(directory, since):
    candidates = [path for path in Path(directory).glob("client_secret*.json")
                  if path.stat().st_mtime >= since]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def setup_credentials(open_url=webbrowser.open, ask=input, report=print_stderr,
                      downloads=Path.home() / "Downloads", clock=time.time):
    started = clock()
    report(t("wizard_intro"))
    ask(t("press_enter"))
    for number, (url, instruction) in enumerate(CONSOLE_STEPS, 1):
        report(t("wizard_step", number=number, total=len(CONSOLE_STEPS), instruction=t(instruction), url=url))
        open_url(url)
        ask(t("press_enter"))
    while True:
        answer = ask(t("ask_client_secret"))
        if answer.strip():
            found = Path(answer.strip().strip("'\"")).expanduser()
        else:
            found = find_client_secret(downloads, started)
            if found is None:
                report(t("no_new_client_secret"))
                continue
        if found.exists() and valid_client_secret(found):
            break
        report(t("not_desktop_client"))
    CLIENT_SECRET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(found, CLIENT_SECRET)
    CLIENT_SECRET.chmod(0o600)


def youtube_client():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit(f"{t('upload_deps_missing')}\n{t('reinstall')}")
    if not CLIENT_SECRET.exists():
        if sys.stdin.isatty() and sys.stderr.isatty():
            setup_credentials()
        else:
            steps = "\n".join(f"  {n}. {url} > {t(key)}" for n, (url, key) in enumerate(CONSOLE_STEPS, 1))
            sys.exit(t("credential_missing", steps=steps, path=CLIENT_SECRET))
    credentials = None
    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        if not credentials.has_scopes(SCOPES):
            credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            credentials = None
    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        credentials = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(credentials.to_json())
    TOKEN_FILE.chmod(0o600)
    return build("youtube", "v3", credentials=credentials)


def upload_video(client, path, title, progress=None, stop=None):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {"snippet": {"title": title}, "status": {"privacyStatus": "private"}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=UPLOAD_CHUNK, resumable=True)
    request = client.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        response = None
        while response is None:
            if stop and stop.is_set():
                raise Cancelled()
            status, response = request.next_chunk()
            if status and progress:
                progress(("step", "upload", status.progress() * 100, None))
    except HttpError as error:
        if any(reason in error.content.decode() for reason in QUOTA_REASONS):
            raise QuotaExceeded() from error
        raise
    return response["id"]


def prepare(path, report, title=None, on_uploaded=None):
    client = youtube_client()
    video_id = known_upload(path)
    if video_id:
        report(t("already_uploaded", video_id=video_id))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / f"{path.stem}.mp4"
            report(t("converting"))
            convert_to_video(path, video)
            report(t("uploading"))
            video_id = upload_video(client, video, title or path.stem)
        remember_upload(path, video_id)
        report(t("uploaded", video_id=video_id))
    if on_uploaded:
        on_uploaded(video_id)
    return video_id


def adopt(video_id, report):
    try:
        client = youtube_client()
    except SystemExit as error:
        report(f"{error}\n{t('shazam_only')}")
        return video_id, None, None, False
    from googleapiclient.errors import HttpError

    try:
        mine, title = video_owner(client, video_id)
    except (SystemExit, HttpError) as error:
        report(f"{error}\n{t('shazam_only')}")
        return video_id, None, None, False
    if mine:
        return video_id, None, None, True
    copy = load_uploads().get(remote_key(video_id))
    if copy:
        report(t("already_uploaded", video_id=copy))
        return copy, downloaded_audio(video_id), title, True
    audio = download_foreign(video_id)
    if audio is None:
        report(t("download_failed_shazam"))
        return video_id, None, title, False
    try:
        uploaded = prepare(audio, report, title, lambda copy: remember(remote_key(video_id), copy))
    except QuotaExceeded:
        report(t("quota_shazam"))
        return video_id, audio, title, False
    return uploaded, audio, title, True


def delete_video(client, video_id):
    client.videos().delete(id=video_id).execute()


def offer_delete(video_id):
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    if video_id not in load_uploads().values():
        return
    print(t("ask_delete", video_id=video_id), end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        print(file=sys.stderr)
        return
    if answer != t("yes"):
        return
    delete_video(youtube_client(), video_id)
    forget_upload(video_id)
    print(t("deleted"), file=sys.stderr)
