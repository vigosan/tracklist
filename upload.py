import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import tracklist

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
            sys.exit(f"YouTube no pudo procesar el video {video_id} ({status})")
        report(f"Procesando en YouTube ({status})")
        sleep(PROCESSING_POLL)


def wait_for_claims(video_id, report, sleep=time.sleep, clock=time.monotonic):
    began = clock()
    counts = []
    while True:
        session, entries = tracklist.try_claims(video_id)
        if session is None:
            sys.exit(
                "Sin sesion de Studio: entra en studio.youtube.com con Brave y vuelve a lanzar "
                f"el mismo comando (el video ya esta subido: https://youtu.be/{video_id})"
            )
        counts.append(len(entries))
        elapsed = clock() - began
        report(f"Reclamaciones: {len(entries)} ({tracklist.hhmmss(int(elapsed))})")
        if elapsed >= CLAIMS_TIMEOUT:
            report(f"Content ID sigue trabajando: vuelve a lanzar 'tracklist {video_id}' mas tarde")
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


def remember_upload(path, video_id):
    uploads = load_uploads()
    uploads[file_hash(path)] = video_id
    UPLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_FILE.write_text(json.dumps(uploads))


def forget_upload(video_id):
    uploads = {digest: known for digest, known in load_uploads().items() if known != video_id}
    UPLOADS_FILE.write_text(json.dumps(uploads))


def youtube_client():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit(
            "Para subir audio ejecuta con el python del venv.\n"
            "Dependencias (una vez): .venv/bin/python -m pip install google-api-python-client google-auth-oauthlib"
        )
    if not CLIENT_SECRET.exists():
        sys.exit(
            "Falta la credencial de YouTube. Una sola vez:\n"
            "  1. console.cloud.google.com > proyecto nuevo\n"
            "  2. APIs y servicios > activa 'YouTube Data API v3'\n"
            "  3. Pantalla de consentimiento OAuth > externo, anadete como usuario de prueba\n"
            "  4. Credenciales > ID de cliente OAuth > 'App de escritorio' > descargar JSON\n"
            f"  5. Guardalo como {CLIENT_SECRET}"
        )
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
            sys.exit("Cuota de subidas de YouTube agotada: prueba manana")
        raise
    return response["id"]


def prepare(path, report):
    client = youtube_client()
    video_id = known_upload(path)
    if video_id:
        report(f"Ya subido como https://youtu.be/{video_id}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / f"{path.stem}.mp4"
            report("Convirtiendo a video")
            convert_to_video(path, video)
            report("Subiendo a YouTube")
            video_id = upload_video(client, video, path.stem)
        remember_upload(path, video_id)
        report(f"Subido como https://youtu.be/{video_id}")
    wait_for_processing(client, video_id, report)
    wait_for_claims(video_id, report)
    return video_id


def delete_video(client, video_id):
    client.videos().delete(id=video_id).execute()


def offer_delete(video_id):
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    if video_id not in load_uploads().values():
        return
    print(f"¿Borrar el video de YouTube {video_id}? [s/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        print(file=sys.stderr)
        return
    if answer != "s":
        return
    delete_video(youtube_client(), video_id)
    forget_upload(video_id)
    print("Video borrado.", file=sys.stderr)
