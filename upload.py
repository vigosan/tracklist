import subprocess

import tracklist


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
