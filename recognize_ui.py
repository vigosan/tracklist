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
