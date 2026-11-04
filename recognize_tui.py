import asyncio
import subprocess
import time

import recognize_ui
import tracklist

SAMPLE_ESTIMATE = 4.0
SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"


def status_fields(state, now):
    total = state["total"]
    remaining = max(0, int(round((state.get("backoff_until") or 0) - now)))
    if remaining:
        fraction = 0.0
    else:
        fraction = min(0.95, (now - state["sample_started"]) / SAMPLE_ESTIMATE)
    if state.get("phase") == "done":
        percent = 100
    else:
        percent = min(100, int(100 * (state["sample"] + fraction) / total)) if total else 0
    gap = state["gap"]
    return {
        "gap": f"{gap[0]}/{gap[1]}" if gap else "-",
        "percent": percent,
        "sample": f"{min(state['sample'] + 1, total)}/{total}" if total else "-",
        "suggestions": state["suggestions"],
        "elapsed": tracklist.hhmmss(int((state.get("ended") or now) - state["began"])),
        "backoff": remaining,
    }


PHASE_LABELS = {
    "cookies": "Leyendo cookies del navegador...",
    "claims": "Pidiendo reclamaciones a Studio...",
    "video": "Pidiendo datos del video...",
    "probe": "Leyendo duracion del video...",
}


def phase_label(phase):
    return PHASE_LABELS.get(phase, "")


def status_text(state, fields):
    if state["paused"]:
        return "PAUSA"
    if state.get("phase") == "done":
        count = fields["suggestions"]
        if not count:
            return "Completado - sin sugerencias"
        noun = "sugerencia" if count == 1 else "sugerencias"
        return f"Completado - {count} {noun}"
    phase = phase_label(state.get("phase"))
    if phase:
        return phase
    if fields["backoff"]:
        return f"rate limit - backoff {fields['backoff']}s"
    return f"Muestra {fields['sample']}"


def initial_state(now):
    return {
        "gap": None, "sample": 0, "total": 0, "sample_started": now,
        "backoff": False, "backoff_until": None, "suggestions": 0, "began": now,
        "phase": "cookies", "paused": False, "ended": None,
    }


def run_app(video_id, duration, label):
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Input, Label, ProgressBar, RichLog, Static

    class RecognizeApp(App):
        CSS = """
        Screen { layout: vertical; }
        #body { height: 1fr; }
        #tracklist { width: 2fr; border: round $accent; padding: 0 1; }
        #side { width: 1fr; }
        #status { height: auto; border: round $accent; padding: 0 1; }
        #log { height: 1fr; border: round $accent; }
        """
        BINDINGS = [
            ("p", "pause", "Pausa"),
            ("r", "refresh", "Refrescar"),
            ("n", "new", "Nuevo"),
            ("x", "clear", "Vaciar"),
            ("c", "copy", "Copiar"),
            ("q", "quit", "Salir"),
            ("s", "confirm", "Confirmar"),
            ("escape", "cancel", "Cancelar"),
        ]

        def __init__(self, video_id, duration, label):
            super().__init__()
            self.video_id = video_id
            self.entries = []
            self.gaps = []
            self.duration = duration
            self.label = label
            self.title_line = None
            self.error = None
            self.session = None
            self.suggestions = []
            self.rows_state = {"completed": {}, "active": None, "samples": []}
            self.state = initial_state(time.monotonic())
            self.frame = 0
            self.resume = asyncio.Event()
            self.resume.set()
            self.pending = None
            self.loaded = False
            self.typed = False

        def compose(self) -> ComposeResult:
            with Horizontal(id="body"):
                with VerticalScroll(id="tracklist"):
                    yield Static(id="rows")
                with Vertical(id="side"):
                    with Vertical(id="status"):
                        yield Label(id="status-text")
                        yield ProgressBar(total=100, show_eta=False, id="bar")
                        yield Input(placeholder="URL o ID", id="video-input")
                    yield RichLog(id="log")
            yield Footer()

        def on_mount(self):
            self.query_one("#tracklist", VerticalScroll).border_title = "Tracklist"
            self.query_one("#status", Vertical).border_title = "Estado"
            self.query_one("#log", RichLog).border_title = "Log"
            self.query_one("#video-input", Input).display = False
            self.set_interval(0.1, self.refresh_view)
            self.run_worker(self.load_and_recognize(), exclusive=True)

        async def load_and_recognize(self):
            try:
                await self.load()
            except SystemExit as failure:
                self.fail(str(failure.code))
                return
            except Exception as failure:
                self.fail(f"Error inesperado: {failure}")
                return
            if self.error is not None:
                self.fail(self.error)
                return
            if not self.gaps:
                self.exit()
                return
            await self.recognize()

        def fail(self, message):
            if self.loaded:
                self.error = None
                self.query_one("#log", RichLog).write(f"[!] {message}")
                return
            self.error = message
            self.exit()

        async def load(self):
            self.state["phase"] = "claims"
            self.log_line("[*] consultando Content ID en YouTube Studio...")
            self.session, self.entries = await asyncio.to_thread(
                tracklist.try_claims, self.video_id
            )
            if self.typed and self.session is None:
                self.log_line(
                    "[!] si el video no es tuyo, sal y lanza 'tracklist <url>' "
                    "para subirlo y leer Content ID"
                )
            self.on_progress(("claims", len(self.entries)))
            self.state["phase"] = "video" if self.entries else "probe"
            if self.duration is None:
                await self.resolve_duration()
            if self.duration is None:
                self.error = "No se pudo leer la duracion del video."
                return
            if self.entries:
                self.gaps = tracklist.find_gaps(self.entries, self.duration)
            else:
                self.gaps = tracklist.scan_gaps(self.duration)
            missing = sum(end - start for start, end in self.gaps)
            self.log_line(
                f"[*] {tracklist.hhmmss(self.duration)} de video, "
                f"{len(self.gaps)} huecos ({tracklist.hhmmss(missing)}) por analizar"
            )
            self.state["phase"] = "recognizing"
            self.loaded = True

        def log_line(self, text):
            self.query_one("#log", RichLog).write(text)

        async def resolve_duration(self):
            cached = tracklist.load_cache(self.video_id)
            if cached["duration"]:
                self.title_line = cached["title"]
                self.duration = cached["duration"]
                return
            if self.session is not None:
                info = (await asyncio.to_thread(
                    tracklist.fetch_videos, [self.video_id], self.session
                )).get(self.video_id)
                if info:
                    self.title_line = info["title"]
                    self.duration = info["length"] or None
            if self.duration is None:
                self.duration = await asyncio.to_thread(
                    tracklist.probe_duration, self.video_id
                )
            if self.duration is not None:
                await asyncio.to_thread(
                    tracklist.save_video_details,
                    self.video_id, self.title_line, self.duration,
                )

        async def recognize(self):
            await tracklist.recognize_gaps_async(
                self.video_id, self.gaps, self.on_progress, self.resume
            )
            self.state["phase"] = "done"

        def on_progress(self, event):
            kind = event[0]
            if kind == "gap":
                self.close_active()
                _, index, total, start, end = event
                planned = len(tracklist.plan_samples(start, end))
                self.rows_state["active"] = {
                    "index": index - 1, "sample": 0, "total": planned,
                    "matches": [], "backoff": 0,
                }
                self.state.update(gap=(index, total), sample=0, total=planned,
                                  sample_started=time.monotonic(), backoff_until=None)
            elif kind == "sample":
                _, offset, match = event
                self.rows_state["samples"].append((offset, match))
                self.state["backoff_until"] = None
                if self.rows_state["active"] is not None:
                    self.rows_state["active"]["sample"] += 1
                    self.rows_state["active"]["matches"] = tracklist.merge_samples(self.rows_state["samples"])
                self.state.update(sample=self.state["sample"] + 1,
                                  sample_started=time.monotonic())
            elif kind == "backoff":
                _, seconds, _attempt = event
                self.state["backoff_until"] = time.monotonic() + seconds
            elif kind == "resolved":
                self.state["backoff_until"] = None
            elif kind == "done":
                self.close_active()
                self.state["gap"] = None
                self.state["ended"] = time.monotonic()
            line = recognize_ui.pentest_line(event)
            if line:
                self.query_one("#log", RichLog).write(line)

        def close_active(self):
            active = self.rows_state["active"]
            if active is not None:
                merged = tracklist.merge_samples(self.rows_state["samples"])
                self.rows_state["completed"][active["index"]] = merged
                self.suggestions += merged
                self.state["suggestions"] = len(self.suggestions)
                self.rows_state["active"] = None
                self.rows_state["samples"] = []

        def refresh_view(self):
            self.frame += 1
            spinner = SPINNER[self.frame % len(SPINNER)]
            fields = status_fields(self.state, time.monotonic())
            if self.rows_state["active"] is not None:
                self.rows_state["active"]["backoff"] = fields["backoff"]
            rows = recognize_ui.tui_rows(
                self.entries, self.gaps, self.rows_state["completed"], self.rows_state["active"]
            )
            rows = [row.replace("...[/cyan]", f" {spinner}[/cyan]") for row in rows]
            self.query_one("#rows", Static).update("\n".join(rows))
            self.query_one("#tracklist", VerticalScroll).scroll_end(animate=False)
            estado = status_text(self.state, fields)
            self.query_one("#status-text", Label).update(
                f"Hueco {fields['gap']}   {estado}\n"
                f"Sugerencias {fields['suggestions']}   Tiempo {fields['elapsed']}"
            )
            self.query_one("#bar", ProgressBar).update(progress=fields["percent"])

        def action_copy(self):
            if self.pending is not None:
                return
            found = self.suggestions + tracklist.merge_samples(self.rows_state["samples"])
            entries = sorted(
                self.entries + tracklist.drop_claimed(self.entries, found),
                key=lambda entry: entry["start"],
            )
            text = tracklist.render(
                entries, tracklist.find_gaps(entries, self.duration),
                self.video_id, self.label, self.title_line,
            )
            log = self.query_one("#log", RichLog)
            try:
                subprocess.run(["pbcopy"], input=text.encode(), check=True)
                log.write("[*] copiado al portapapeles")
            except (OSError, subprocess.CalledProcessError):
                log.write("[!] no se pudo copiar al portapapeles")

        def action_pause(self):
            if self.pending is not None:
                return
            log = self.query_one("#log", RichLog)
            if self.state["paused"]:
                self.state["paused"] = False
                self.resume.set()
                log.write("[*] reanudado")
            else:
                self.state["paused"] = True
                self.resume.clear()
                log.write("[*] pausa")

        def action_refresh(self):
            if self.pending is not None:
                return
            self.restart()

        def action_new(self):
            if self.pending is not None:
                return
            self.ask_or_run("new")

        def action_clear(self):
            if self.pending is not None:
                return
            self.ask_or_run("clear")

        def ask_or_run(self, pending):
            if self.suggestions or self.rows_state["samples"]:
                self.pending = pending
                self.query_one("#log", RichLog).write(
                    f"[!] descartar {len(self.suggestions) + len(tracklist.merge_samples(self.rows_state['samples']))} sugerencias? s = si, escape = no"
                )
                return
            self.apply_pending(pending)

        def action_confirm(self):
            if self.pending is None:
                return
            pending, self.pending = self.pending, None
            self.apply_pending(pending)

        def action_cancel(self):
            if self.pending is None:
                return
            self.pending = None
            self.query_one("#log", RichLog).write("[*] cancelado")

        def apply_pending(self, pending):
            if pending == "new":
                self.query_one("#log", RichLog).write("[*] escribe la URL o ID y pulsa Enter")
                self.query_one("#video-input", Input).display = True
                self.query_one("#video-input", Input).focus()
                return
            self.restart()

        def restart(self):
            self.workers.cancel_all()
            self.suggestions = []
            self.rows_state = {"completed": {}, "active": None, "samples": []}
            self.state = initial_state(time.monotonic())
            self.resume.set()
            self.query_one("#log", RichLog).clear()
            self.run_worker(self.load_and_recognize(), exclusive=True)

        def on_input_submitted(self, event):
            new_id = tracklist.parse_video_id(event.value.strip())
            event.input.display = False
            event.input.value = ""
            if not new_id:
                return
            self.video_id = new_id
            self.typed = True
            self.entries = []
            self.gaps = []
            self.title_line = None
            self.duration = None
            self.restart()

    app = RecognizeApp(video_id, duration, label)
    app.run()
    app.close_active()
    return app.suggestions, app.entries, app.gaps, app.duration, app.title_line, app.error
