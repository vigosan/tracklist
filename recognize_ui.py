import sys

import tracklist
from messages import t


def pentest_line(event):
    kind = event[0]
    if kind == "gap":
        _, index, total, start, end = event
        return t("log_gap", index=index, total=total, start=tracklist.hhmmss(start), end=tracklist.hhmmss(end))
    if kind == "sample":
        _, offset, match = event
        if match is None:
            return t("log_no_match", offset=tracklist.hhmmss(offset))
        return f"[+] {tracklist.hhmmss(offset)} {match['artist']} - {match['title']}"
    if kind == "claims":
        count = event[1]
        if not count:
            return t("log_no_claims")
        return t("log_claim" if count == 1 else "log_claims", count=count)
    if kind == "download":
        stage = event[1]
        if stage == "start":
            return t("log_download_start")
        if stage == "cached":
            return t("log_download_cached")
        return t("log_download_done")
    if kind == "cached":
        _, done, total = event
        return t("log_cached", done=done, total=total, left=total - done)
    if kind == "resolved":
        return t("log_resolved", offset=tracklist.hhmmss(event[1]))
    if kind == "workers":
        count = event[1]
        if count == 1:
            return t("log_workers_down")
        return t("log_workers_up", count=count)
    if kind == "backoff":
        _, seconds, attempt = event
        return t("log_backoff", seconds=seconds, attempt=attempt, retries=tracklist.BACKOFF_RETRIES)
    if kind == "done":
        _, count, seconds = event
        return t("log_done", count=count, elapsed=tracklist.hhmmss(seconds))
    return None


def entry_line(entry):
    artist = entry["artist"].replace("[", r"\[")
    title = entry["title"].replace("[", r"\[")
    line = f"  - {tracklist.hhmmss(entry['start'])} {artist} - {title}"
    if entry.get("suggested"):
        return f"[yellow]{line} (?)[/yellow]"
    return f"[green]{line}[/green]"


def gap_line(start, end):
    return f"[dim]  - {tracklist.hhmmss(start)} {t('render_gap', end=tracklist.hhmmss(end))}[/dim]"


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
                status = t("row_downloading")
            else:
                status = t("row_searching", sample=active["sample"], total=active["total"])
            if active["backoff"]:
                status += f" - backoff {active['backoff']}s"
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
            f"{t('tui_missing')}\n{t('reinstall')}",
            file=sys.stderr,
        )
        return False
    return True


def offer_clipboard(text):
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    print(t("ask_clipboard"), end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        print(file=sys.stderr)
        return
    if answer not in ("", t("yes")):
        return
    if tracklist.copy_to_clipboard(text):
        print(t("copied"), file=sys.stderr)
    else:
        print(t("copy_failed"), file=sys.stderr)
