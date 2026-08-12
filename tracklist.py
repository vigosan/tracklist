#!/Users/vicent/Desktop/tracklist/.venv/bin/python3
"""Genera un tracklist con timestamps a partir de las reclamaciones de
Content ID que YouTube Studio muestra para un video propio."""

import argparse
import asyncio
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path

BASE = "https://studio.youtube.com/youtubei/v1/creator"
ORIGIN = "https://studio.youtube.com"
CLAIMS_ENDPOINT = f"{BASE}/list_creator_received_claims?alt=json"
VIDEOS_ENDPOINT = f"{BASE}/get_creator_videos?alt=json"
SESSION_FILE = Path.home() / ".config" / "tracklist" / "session.json"
MIN_GAP_SECONDS = 90
SAMPLE_SECONDS = 10
SAMPLE_INTERVAL = 60
SAMPLE_LEAD = 15
DROP_HEADERS = {"content-length", "host", "x-youtube-ad-signals", "priority"}


def parse_curl(text):
    tokens = shlex.split(text.replace("\\\n", " "))
    headers = {}
    body = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-H", "--header"):
            i += 1
            name, _, value = tokens[i].partition(":")
            headers[name.strip().lower()] = value.strip()
        elif token in ("-b", "--cookie"):
            i += 1
            headers["cookie"] = tokens[i]
        elif token in ("--data", "--data-raw", "-d", "--data-binary"):
            i += 1
            body = tokens[i]
        i += 1
    if "cookie" not in headers:
        sys.exit("El cURL no incluye cookies: copia la peticion con 'Copy as cURL'.")
    if body is None:
        sys.exit("El cURL no incluye cuerpo: asegurate de copiar la peticion POST.")
    headers = {k: v for k, v in headers.items() if k not in DROP_HEADERS}
    return {"headers": headers, "context": json.loads(body)["context"]}


def save_session(curl_path):
    session = parse_curl(Path(curl_path).read_text())
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session))
    SESSION_FILE.chmod(0o600)
    print(f"Sesion guardada en {SESSION_FILE}")


def load_session():
    if not SESSION_FILE.exists():
        sys.exit(
            "No hay sesion guardada. En Studio abre DevTools > Network, despliega\n"
            "las reclamaciones de un video, localiza list_creator_received_claims,\n"
            "'Copy as cURL', guardalo en un fichero y ejecuta:\n"
            "    tracklist --save-session fichero.txt"
        )
    return json.loads(SESSION_FILE.read_text())


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


def refresh_headers(headers, cookies, timestamp):
    return {
        **headers,
        "cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "authorization": build_authorization(cookies, timestamp),
    }


def brave_cookies():
    with tempfile.TemporaryDirectory() as tmp:
        jar_path = Path(tmp) / "cookies.txt"
        command = [
            "yt-dlp", "--quiet", "--no-warnings", "--ignore-errors", "--skip-download",
            "--cookies-from-browser", "brave", "--cookies", str(jar_path),
            "https://www.youtube.com/robots.txt",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit("yt-dlp no esta instalado (brew install yt-dlp), o usa --save-session.")
        if not jar_path.exists():
            sys.exit(
                "No se pudieron leer las cookies de Brave:\n"
                f"{result.stderr.strip()[-300:]}\n"
                "Alternativa manual: captura un cURL y usa --save-session."
            )
        return parse_cookie_file(jar_path)


def fresh_session():
    session = load_session()
    cookies = brave_cookies()
    if "SAPISID" not in cookies:
        sys.exit("Brave no tiene sesion de YouTube iniciada: entra en studio.youtube.com y reintenta.")
    session["headers"] = refresh_headers(session["headers"], cookies, int(time.time()))
    return session


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
        if error.code in (401, 403):
            sys.exit(
                "YouTube rechazo la sesion: entra en studio.youtube.com con Brave"
                " y reintenta, o captura un cURL nuevo y usa --save-session."
            )
        sys.exit(f"Error HTTP {error.code}: {error.read()[:300].decode(errors='replace')}")


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


def plan_samples(start, end):
    return list(range(start + SAMPLE_LEAD, end - SAMPLE_SECONDS + 1, SAMPLE_INTERVAL))


def merge_samples(samples):
    suggestions = []
    previous = None
    for offset, match in samples:
        if match is None:
            previous = None
            continue
        if previous and (previous["artist"], previous["title"]) == (match["artist"], match["title"]):
            previous["duration"] = offset + SAMPLE_SECONDS - previous["start"]
            continue
        previous = {**match, "label": "", "start": offset, "duration": SAMPLE_SECONDS, "suggested": True}
        suggestions.append(previous)
    return suggestions


def download_gap(video_id, start, end, directory):
    command = [
        "yt-dlp", "--quiet", "--no-warnings", "-x", "--audio-format", "mp3",
        "--cookies-from-browser", "brave",
        "--download-sections", f"*{start}-{end}",
        "-o", str(directory / f"gap-{start}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    matches = list(directory.glob(f"gap-{start}.*"))
    if result.returncode != 0 or not matches:
        print(f"Aviso: no se pudo descargar el hueco {hhmmss(start)}-{hhmmss(end)}", file=sys.stderr)
        return None
    return matches[0]


def cut_sample(source, offset, directory):
    target = directory / f"{source.stem}-{offset}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(offset), "-t", str(SAMPLE_SECONDS),
         "-i", str(source), str(target)],
        check=True, capture_output=True,
    )
    return target


def recognize_gaps_async(video_id, gaps, progress):
    try:
        from shazamio import Shazam
        from shazamio.exceptions import FailedDecodeJson
    except ImportError:
        sys.exit(
            "Para --recognize ejecuta con el python del venv:\n"
            "    .venv/bin/python3 tracklist.py --recognize <video>\n"
            "Dependencias (una vez): .venv/bin/pip install audioop-lts shazamio==0.4.0.1"
        )

    async def run():
        began = time.monotonic()
        shazam = Shazam()
        suggestions = []
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index, (start, end) in enumerate(gaps, 1):
                progress(("gap", index, len(gaps), start, end))
                source = download_gap(video_id, start, end, directory)
                if source is None:
                    continue
                samples = []
                for offset in plan_samples(start, end):
                    sample = cut_sample(source, offset - start, directory)
                    try:
                        result = await shazam.recognize_song(str(sample))
                    except FailedDecodeJson:
                        progress(("backoff",))
                        await asyncio.sleep(30)
                        try:
                            result = await shazam.recognize_song(str(sample))
                        except FailedDecodeJson:
                            result = {}
                    track = result.get("track")
                    match = None
                    if track:
                        match = {"artist": track.get("subtitle", "?"), "title": track.get("title", "?")}
                    samples.append((offset, match))
                    progress(("sample", offset, match))
                    await asyncio.sleep(1)
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
            lines.append(
                f"  - {hhmmss(start)} ????? (sin identificar, hasta {hhmmss(item['end'])})"
            )
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
    suggested_part = f"{suggested} sugeridos, " if suggested else ""
    lines += [
        "",
        f"{len(entries) - suggested} identificados, {suggested_part}{len(gaps)} huecos "
        f"({hhmmss(missing)} sin identificar de {hhmmss(identified + missing)})",
    ]
    return "\n".join(lines)


def recognition_available():
    try:
        import shazamio  # noqa: F401
    except ImportError:
        print(
            "Aviso: reconocimiento de huecos desactivado (faltan dependencias).\n"
            "Para activarlo: .venv/bin/python3 tracklist.py <video>\n"
            "(una vez: .venv/bin/pip install audioop-lts shazamio==0.4.0.1 rich)",
            file=sys.stderr,
        )
        return False
    return True


def parse_video_id(text):
    match = re.search(r"(?:v=|youtu\.be/|video/)([\w-]{11})", text)
    return match.group(1) if match else text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", help="URL o ID del video")
    parser.add_argument("--save-session", metavar="FICHERO", help="guarda un cURL copiado de Studio")
    parser.add_argument("--from-json", metavar="FICHERO", help="usa una respuesta ya guardada")
    parser.add_argument("--duration", type=int, help="duracion total en segundos, para el hueco final")
    parser.add_argument("--label", action="store_true", help="muestra el sello de cada track")
    parser.add_argument(
        "--recognize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="identifica los huecos con Shazam (activado por defecto)",
    )
    args = parser.parse_args()

    if args.save_session:
        save_session(args.save_session)
        return
    if not args.video and sys.stdin.isatty() and sys.stderr.isatty():
        print("URL o ID del video: ", end="", file=sys.stderr, flush=True)
        try:
            args.video = input().strip()
        except EOFError:
            print(file=sys.stderr)
    if not args.video:
        parser.error("indica la URL o el ID del video")

    video_id = parse_video_id(args.video)

    title = None
    duration = args.duration
    if args.from_json:
        response = json.loads(Path(args.from_json).read_text())
    else:
        session = fresh_session()
        response = fetch_claims(video_id, session)
        if duration is None:
            info = fetch_videos([video_id], session).get(video_id)
            if info:
                title = info["title"]
                duration = info["length"] or None

    entries = extract(response)
    if not entries:
        sys.exit("Sin reclamaciones con timestamp para este video.")
    gaps = find_gaps(entries, duration)
    if args.recognize and gaps and recognition_available():
        import recognize_ui

        if recognize_ui.textual_available():
            import recognize_tui

            found = recognize_tui.run_app(video_id, entries, gaps, duration, args.label, title)
        else:
            found = recognize_gaps(video_id, gaps, recognize_ui._pentest_progress)
        entries = sorted(entries + found, key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, video_id, args.label, title)
        print(text)
        recognize_ui.offer_clipboard(text)
        return
    print(render(entries, gaps, video_id, args.label, title))


if __name__ == "__main__":
    main()
