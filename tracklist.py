#!/usr/bin/env python3
"""Genera un tracklist con timestamps a partir de las reclamaciones de
Content ID que YouTube Studio muestra para un video propio."""

import argparse
import json
import re
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://studio.youtube.com/youtubei/v1/creator"
CLAIMS_ENDPOINT = f"{BASE}/list_creator_received_claims?alt=json"
VIDEOS_ENDPOINT = f"{BASE}/get_creator_videos?alt=json"
SESSION_FILE = Path.home() / ".config" / "tracklist" / "session.json"
MIN_GAP_SECONDS = 90
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
            sys.exit("Sesion caducada: vuelve a capturar el cURL y --save-session.")
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
        if show_label and item["label"]:
            line += f"  [{item['label']}]"
        lines.append(line)
    identified = sum(entry["duration"] for entry in entries)
    missing = sum(end - start for start, end in gaps)
    lines += [
        "",
        f"{len(entries)} identificados, {len(gaps)} huecos "
        f"({hhmmss(missing)} sin identificar de {hhmmss(identified + missing)})",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", help="URL o ID del video")
    parser.add_argument("--save-session", metavar="FICHERO", help="guarda un cURL copiado de Studio")
    parser.add_argument("--from-json", metavar="FICHERO", help="usa una respuesta ya guardada")
    parser.add_argument("--duration", type=int, help="duracion total en segundos, para el hueco final")
    parser.add_argument("--label", action="store_true", help="muestra el sello de cada track")
    args = parser.parse_args()

    if args.save_session:
        save_session(args.save_session)
        return
    if not args.video:
        parser.error("indica la URL o el ID del video")

    match = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", args.video)
    video_id = match.group(1) if match else args.video

    title = None
    duration = args.duration
    if args.from_json:
        response = json.loads(Path(args.from_json).read_text())
    else:
        session = load_session()
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
    print(render(entries, gaps, video_id, args.label, title))


if __name__ == "__main__":
    main()
