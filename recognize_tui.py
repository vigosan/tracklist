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


def run_app(video_id, entries, gaps, duration, label, title):
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

        def __init__(self, video_id, entries, gaps, duration, label, title):
            super().__init__()
            self.video_id = video_id
            self.entries = entries
            self.gaps = gaps
            self.duration = duration
            self.label = label
            self.title_line = title
            self.suggestions = []
            self.rows_state = {"completed": {}, "active": None, "samples": []}
            self.state = {
                "gap": None, "sample": 0, "total": 0, "sample_started": time.monotonic(),
                "backoff": False, "suggestions": 0, "began": time.monotonic(),
            }
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
            self.run_worker(self.recognize(), exclusive=True)

        async def recognize(self):
            await tracklist.recognize_gaps_async(
                self.video_id, self.gaps, self.on_progress
            )
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
            estado = "rate limit - backoff" if fields["backoff"] else f"Muestra {fields['sample']}"
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

    app = RecognizeApp(video_id, entries, gaps, duration, label, title)
    app.run()
    app.close_active()
    return app.suggestions
