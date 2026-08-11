import contextlib
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
    if kind == "backoff":
        return "[!] rate limit - backoff 30s"
    if kind == "done":
        _, count, seconds = event
        return f"[*] {count} sugerencias en {tracklist.hhmmss(seconds)}"
    return None


def entry_line(entry):
    line = f"  - {tracklist.hhmmss(entry['start'])} {entry['artist']} - {entry['title']}"
    if entry.get("suggested"):
        return f"[yellow]{line} (?)[/yellow]"
    return f"[green]{line}[/green]"


def gap_line(start, end):
    return f"[dim]  - {tracklist.hhmmss(start)} ????? (sin identificar, hasta {tracklist.hhmmss(end)})[/dim]"


def gap_residuals(start, end, suggestions):
    return tracklist.find_gaps([{"start": start, "duration": 0}] + suggestions, end)


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


@contextlib.contextmanager
def presenter(entries, gaps):
    if not sys.stderr.isatty():
        yield _pentest_progress
        return
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.text import Text
    except ImportError:
        sys.exit("Falta rich para el TUI de --recognize: .venv/bin/pip install rich")
    state = {"completed": {}, "active": None, "samples": []}

    def close_active():
        if state["active"] is not None:
            state["completed"][state["active"]["index"]] = tracklist.merge_samples(state["samples"])
            state["active"] = None
            state["samples"] = []

    with Live(console=Console(stderr=True), refresh_per_second=8) as live:

        def refresh():
            live.update(Text.from_markup("\n".join(tui_rows(entries, gaps, state["completed"], state["active"]))))

        def progress(event):
            kind = event[0]
            if kind == "gap":
                close_active()
                _, index, total, start, end = event
                state["active"] = {
                    "index": index - 1,
                    "sample": 0,
                    "total": len(tracklist.plan_samples(start, end)),
                    "matches": [],
                    "backoff": False,
                }
            elif kind == "sample":
                _, offset, match = event
                state["samples"].append((offset, match))
                state["active"]["sample"] += 1
                state["active"]["backoff"] = False
                state["active"]["matches"] = tracklist.merge_samples(state["samples"])
            elif kind == "backoff":
                state["active"]["backoff"] = True
            elif kind == "done":
                close_active()
            refresh()

        refresh()
        yield progress


def offer_clipboard(text):
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    print("¿Copiar al portapapeles? [S/n] ", end="", file=sys.stderr, flush=True)
    answer = input().strip().lower()
    if answer not in ("", "s"):
        return
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        print("Copiado.", file=sys.stderr)
    except (OSError, subprocess.CalledProcessError):
        print("Aviso: no se pudo copiar al portapapeles", file=sys.stderr)
