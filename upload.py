import subprocess
import sys
import time

import tracklist

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
        if status == "failed":
            sys.exit(f"YouTube no pudo procesar el video {video_id}")
        report(f"Procesando en YouTube ({status})")
        sleep(PROCESSING_POLL)


def wait_for_claims(video_id, report, sleep=time.sleep, clock=time.monotonic):
    began = clock()
    counts = []
    while True:
        session, entries = tracklist.try_claims(video_id)
        if session is None:
            report("Sin sesion de Studio: no se pueden leer las reclamaciones, sigo sin esperar")
            return
        counts.append(len(entries))
        elapsed = clock() - began
        report(f"Reclamaciones: {len(entries)} ({tracklist.hhmmss(int(elapsed))})")
        if elapsed >= CLAIMS_TIMEOUT:
            report(f"Content ID sigue trabajando: vuelve a lanzar 'tracklist {video_id}' mas tarde")
            return
        if claims_settled(counts, elapsed):
            return
        sleep(CLAIMS_POLL)
