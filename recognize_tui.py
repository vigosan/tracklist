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
}


def phase_label(phase):
    return PHASE_LABELS.get(phase, "")


def initial_state(now):
    return {
        "gap": None, "sample": 0, "total": 0, "sample_started": now,
        "backoff": False, "suggestions": 0, "began": now,
        "phase": "cookies", "paused": False,
    }


def run_app(video_id, duration, label):
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Label, ProgressBar, RichLog, Static

    class RecognizeApp(App):
        CSS = """
        Screen { layout: vertical; }
        #body { height: 1fr; }
        #tracklist { width: 2fr; border: round $accent; padding: 0 1; }
        #side { width: 1fr; }
        #status { height: auto; border: round $accent; padding: 0 1; }
        #log { height: 1fr; border: round $accent; }
        """
        BINDINGS = [("c", "copy", "Copiar"), ("q", "quit", "Salir")]

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

        def compose(self) -> ComposeResult:
            with Horizontal(id="body"):
                with VerticalScroll(id="tracklist"):
                    yield Static(id="rows")
                with Vertical(id="side"):
                    with Vertical(id="status"):
                        yield Label(id="status-text")
                        yield ProgressBar(total=100, show_eta=False, id="bar")
                    yield RichLog(id="log")
            yield Footer()

        def on_mount(self):
            self.query_one("#tracklist", VerticalScroll).border_title = "Tracklist"
            self.query_one("#status", Vertical).border_title = "Estado"
            self.query_one("#log", RichLog).border_title = "Log"
            self.set_interval(0.1, self.refresh_view)
            self.run_worker(self.load_and_recognize(), exclusive=True)

        async def load_and_recognize(self):
            self.state["phase"] = "cookies"
            if self.session is None:
                self.session = await asyncio.to_thread(tracklist.fresh_session)
            self.state["phase"] = "claims"
            response = await asyncio.to_thread(tracklist.fetch_claims, self.video_id, self.session)
            self.entries = tracklist.extract(response)
            if not self.entries:
                self.error = "Sin reclamaciones con timestamp para este video."
                self.exit()
                return
            self.state["phase"] = "video"
            if self.duration is None:
                info = (await asyncio.to_thread(
                    tracklist.fetch_videos, [self.video_id], self.session
                )).get(self.video_id)
                if info:
                    self.title_line = info["title"]
                    self.duration = info["length"] or None
            self.gaps = tracklist.find_gaps(self.entries, self.duration)
            self.state["phase"] = "recognizing"
            if not self.gaps:
                self.exit()
                return
            await self.recognize()

        async def recognize(self):
            await tracklist.recognize_gaps_async(self.video_id, self.gaps, self.on_progress)
            self.exit()

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
            phase = phase_label(self.state["phase"])
            if phase:
                estado = phase
            elif fields["backoff"]:
                estado = "rate limit - backoff"
            else:
                estado = f"Muestra {fields['sample']}"
            self.query_one("#status-text", Label).update(
                f"Hueco {fields['gap']}   {estado}\n"
                f"Sugerencias {fields['suggestions']}   Tiempo {fields['elapsed']}"
            )
            self.query_one("#bar", ProgressBar).update(progress=fields["percent"])

        def action_copy(self):
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

    app = RecognizeApp(video_id, duration, label)
    app.run()
    app.close_active()
    return app.suggestions, app.entries, app.gaps, app.duration, app.title_line, app.error
