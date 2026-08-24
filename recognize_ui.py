import subprocess
import sys

import tracklist


def pentest_line(event):
    kind = event[0]
    if kind == "gap":
        _, index, total, start, end = event
        return f"[*] hueco {index}/{total} ({tracklist.hhmmss(start)}-{tracklist.hhmmss(end)})"
    if kind == "sample":
        _, offset, match = event
        if match is None:
            return f"[-] {tracklist.hhmmss(offset)} sin match"
        return f"[+] {tracklist.hhmmss(offset)} {match['artist']} - {match['title']}"
    if kind == "claims":
        count = event[1]
        if not count:
            return "[*] sin reclamaciones: se analiza el video entero con Shazam"
        noun = "reclamacion" if count == 1 else "reclamaciones"
        return f"[*] {count} {noun} de Content ID"
    if kind == "download":
        stage = event[1]
        if stage == "start":
            return "[*] descargando audio..."
        if stage == "cached":
            return "[*] audio ya descargado"
        return "[*] audio descargado"
    if kind == "cached":
        _, done, total = event
        return f"[*] {done}/{total} muestras ya analizadas, faltan {total - done}"
    if kind == "resolved":
        return f"[*] {tracklist.hhmmss(event[1])} recuperado tras el backoff"
    if kind == "backoff":
        return "[!] rate limit - esperando para reintentar"
    if kind == "done":
        _, count, seconds = event
        return f"[*] {count} sugerencias en {tracklist.hhmmss(seconds)}"
    return None


def entry_line(entry):
    artist = entry["artist"].replace("[", r"\[")
    title = entry["title"].replace("[", r"\[")
    line = f"  - {tracklist.hhmmss(entry['start'])} {artist} - {title}"
    if entry.get("suggested"):
        return f"[yellow]{line} (?)[/yellow]"
    return f"[green]{line}[/green]"


def gap_line(start, end):
    return f"[dim]  - {tracklist.hhmmss(start)} ????? (sin identificar, hasta {tracklist.hhmmss(end)})[/dim]"


def gap_residuals(start, end, suggestions):
    return tracklist.find_gaps([{"start": 0, "duration": start}] + suggestions, end)


def tui_rows(entries, gaps, completed, active):
    rows = [(entry["start"], entry_line(entry)) for entry in entries]
    for index, (start, end) in enumerate(gaps):
        if index in completed:
            rows += [(s["start"], entry_line(s)) for s in completed[index]]
            rows += [(rs, gap_line(rs, re)) for rs, re in gap_residuals(start, end, completed[index])]
        elif active is not None and index == active["index"]:
            if active["sample"] == 0:
                status = "descargando"
            else:
                status = f"buscando (muestra {active['sample']}/{active['total']})"
            if active["backoff"]:
                status += " - backoff 30s"
            rows.append((start, f"[cyan]  - {tracklist.hhmmss(start)} {status}...[/cyan]"))
            rows += [(s["start"], entry_line(s)) for s in active["matches"]]
        else:
            rows.append((start, gap_line(start, end)))
    return [line for _, line in sorted(rows, key=lambda row: row[0])]


def _pentest_progress(event):
    line = pentest_line(event)
    if line:
        print(line, file=sys.stderr)


def textual_available():
    if not sys.stderr.isatty():
        return False
    try:
        import textual  # noqa: F401
    except ImportError:
        print(
            "Aviso: TUI desactivado (falta textual: .venv/bin/pip install textual).",
            file=sys.stderr,
        )
        return False
    return True


def offer_clipboard(text):
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    print("¿Copiar al portapapeles? [S/n] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        print(file=sys.stderr)
        return
    if answer not in ("", "s"):
        return
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        print("Copiado.", file=sys.stderr)
    except (OSError, subprocess.CalledProcessError):
        print("Aviso: no se pudo copiar al portapapeles", file=sys.stderr)
