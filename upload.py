import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
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


def download_foreign(video_id):
    existing = downloaded_audio(video_id)
    if existing:
        return existing
    downloads_dir().mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["yt-dlp", "--quiet", "--no-warnings", "--progress", *DOWNLOAD_OPTIONS,
         "--cookies-from-browser", tracklist.current_browser(),
         "-o", str(downloads_dir() / f"{video_id}.%(ext)s"),
         f"https://www.youtube.com/watch?v={video_id}"],
    )
    if result.returncode != 0:
        return None
    return downloaded_audio(video_id)


def convert_to_video(source, target):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:r=1",
         "-i", str(source),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-shortest", str(target)],
        check=True, capture_output=True,
    )


def local_duration(source):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(source)],
        check=True, capture_output=True, text=True,
    )
    return int(float(result.stdout))


def place_audio(source, video_id):
    target = tracklist.audio_path(video_id)
    if target.exists():
        return
    tracklist.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn",
         "-codec:a", "libmp3lame", "-q:a", "2", str(target)],
        check=True, capture_output=True,
    )


def claims_settled(counts, elapsed):
    if elapsed >= CLAIMS_TIMEOUT:
        return True
    recent = counts[-CLAIMS_STABLE_POLLS:]
    return len(recent) == CLAIMS_STABLE_POLLS and recent[0] > 0 and len(set(recent)) == 1


def processing_status(client, video_id):
    items = client.videos().list(part="processingDetails", id=video_id).execute().get("items")
    if not items:
        return "processing"
    return items[0]["processingDetails"]["processingStatus"]


def wait_for_processing(client, video_id, report, sleep=time.sleep):
    while True:
        status = processing_status(client, video_id)
        if status == "succeeded":
            return
        if status != "processing":
            sys.exit(t("processing_failed", video_id=video_id, status=status))
        report(t("processing", status=status))
        sleep(PROCESSING_POLL)


def wait_for_claims(video_id, report, sleep=time.sleep, clock=time.monotonic):
    began = clock()
    counts = []
    while True:
        session, entries = tracklist.try_claims(video_id)
        if session is None:
            sys.exit(t("claims_unreadable", video_id=video_id))
        counts.append(len(entries))
        elapsed = clock() - began
        report(t("claims_count", count=len(entries), elapsed=tracklist.hhmmss(int(elapsed))))
        if elapsed >= CLAIMS_TIMEOUT:
            report(t("content_id_busy"))
            return
        if claims_settled(counts, elapsed):
            return
        sleep(CLAIMS_POLL)


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
        if found and found.exists() and valid_client_secret(found):
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


def upload_video(client, path, title):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {"snippet": {"title": title}, "status": {"privacyStatus": "private"}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=-1, resumable=True)
    request = client.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        response = None
        while response is None:
            _, response = request.next_chunk()
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
    wait_for_processing(client, video_id, report)
    wait_for_claims(video_id, report)
    return video_id


def adopt(video_id, report):
    try:
        client = youtube_client()
    except SystemExit as error:
        report(f"{error}\n{t('shazam_only')}")
        return video_id, None, None
    from googleapiclient.errors import HttpError

    try:
        mine, title = video_owner(client, video_id)
    except (SystemExit, HttpError) as error:
        report(f"{error}\n{t('shazam_only')}")
        return video_id, None, None
    if mine:
        return video_id, None, None
    audio = download_foreign(video_id)
    if audio is None:
        report(t("download_failed_shazam"))
        return video_id, None, title
    try:
        uploaded = prepare(audio, report, title, lambda copy: remember(remote_key(video_id), copy))
    except QuotaExceeded:
        report(t("quota_shazam"))
        return video_id, None, title
    return uploaded, audio, title


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
