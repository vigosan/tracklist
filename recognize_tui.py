import asyncio
import subprocess
import time

import recognize_ui
import tracklist

SAMPLE_ESTIMATE = 4.0
SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"


def status_fields(state, now):
    total = state["total"]
    if state["backoff"]:
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
        "elapsed": tracklist.hhmmss(int(now - state["began"])),
        "backoff": state["backoff"],
    }


PHASE_LABELS = {
    "cookies": "Leyendo cookies de Brave...",
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
        return "rate limit - backoff"
    return f"Muestra {fields['sample']}"


def initial_state(now):
    return {
        "gap": None, "sample": 0, "total": 0, "sample_started": now,
        "backoff": False, "suggestions": 0, "began": now,
        "phase": "cookies", "paused": False,
    }


def run_app(video_id, duration, label, scan=False):
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
            ("n", "new", "Nuevo"),
            ("x", "clear", "Vaciar"),
            ("c", "copy", "Copiar"),
            ("q", "quit", "Salir"),
            ("s", "confirm", "Confirmar"),
            ("escape", "cancel", "Cancelar"),
        ]

        def __init__(self, video_id, duration, label, scan):
            super().__init__()
            self.scan = scan
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
            if self.scan:
                await self.load_scan()
                return
            self.state["phase"] = "cookies"
            if self.session is None:
                self.session = await asyncio.to_thread(tracklist.fresh_session)
            self.state["phase"] = "claims"
            response = await asyncio.to_thread(tracklist.fetch_claims, self.video_id, self.session)
            self.entries = tracklist.extract(response)
            if not self.entries:
                self.error = "Sin reclamaciones con timestamp para este video."
                return
            self.state["phase"] = "video"
            if self.duration is None:
                cached = tracklist.load_cache(self.video_id)
                if cached["duration"]:
                    self.title_line = cached["title"]
                    self.duration = cached["duration"]
                else:
                    info = (await asyncio.to_thread(
                        tracklist.fetch_videos, [self.video_id], self.session
                    )).get(self.video_id)
                    if info:
                        self.title_line = info["title"]
                        self.duration = info["length"] or None
                        await asyncio.to_thread(
                            tracklist.save_video_details,
                            self.video_id, self.title_line, self.duration,
                        )
            self.gaps = tracklist.find_gaps(self.entries, self.duration)
            self.state["phase"] = "recognizing"
            self.loaded = True

        async def load_scan(self):
            self.state["phase"] = "probe"
            self.entries = []
            if self.duration is None:
                cached = tracklist.load_cache(self.video_id)
                self.duration = cached["duration"]
                self.title_line = cached["title"]
            if self.duration is None:
                self.duration = await asyncio.to_thread(tracklist.probe_duration, self.video_id)
                if self.duration is None:
                    self.error = "No se pudo leer la duracion del video."
                    return
                await asyncio.to_thread(
                    tracklist.save_video_details, self.video_id, self.title_line, self.duration
                )
            self.gaps = tracklist.scan_gaps(self.duration)
            self.state["phase"] = "recognizing"
            self.loaded = True

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
                    "matches": [], "backoff": False,
                }
                self.state.update(gap=(index, total), sample=0, total=planned,
                                  sample_started=time.monotonic(), backoff=False)
            elif kind == "sample":
                _, offset, match = event
                self.rows_state["samples"].append((offset, match))
                self.rows_state["active"]["sample"] += 1
                self.rows_state["active"]["backoff"] = False
                self.rows_state["active"]["matches"] = tracklist.merge_samples(self.rows_state["samples"])
                self.state.update(sample=self.state["sample"] + 1,
                                  sample_started=time.monotonic(), backoff=False)
            elif kind == "backoff":
                self.rows_state["active"]["backoff"] = True
                self.state["backoff"] = True
            elif kind == "done":
                self.close_active()
                self.state["gap"] = None
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
            entries = sorted(
                self.entries + self.suggestions + tracklist.merge_samples(self.rows_state["samples"]),
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
            self.entries = []
            self.gaps = []
            self.title_line = None
            self.duration = None
            self.restart()

    app = RecognizeApp(video_id, duration, label, scan)
    app.run()
    app.close_active()
    return app.suggestions, app.entries, app.gaps, app.duration, app.title_line, app.error
