import asyncio
import subprocess
import threading
import time
from dataclasses import dataclass

import recognize_ui
import tracklist
import upload
from messages import t

SAMPLE_ESTIMATE = 4.0
SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"
STEP_PHASES = ("download", "convert", "upload", "audio")


@dataclass
class Result:
    found: list
    entries: list
    gaps: list
    duration: int | None
    title: str | None
    error: str | None
    interrupted: bool
    video_id: str | None


def since_upload(uploaded_at, wall):
    if uploaded_at is None:
        return None
    return tracklist.hhmmss(upload.claims_elapsed(uploaded_at, wall))


def status_fields(state, now, wall=None):
    total = state["total"]
    remaining = max(0, int(round((state.get("backoff_until") or 0) - now)))
    if remaining:
        fraction = 0.0
    else:
        fraction = min(0.95, (now - state["sample_started"]) / SAMPLE_ESTIMATE)
    if state.get("phase") == "done":
        percent = 100
    elif state.get("phase") in STEP_PHASES:
        percent = int(state.get("step_percent") or 0)
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
        "since_upload": since_upload(state.get("uploaded_at"), time.time() if wall is None else wall),
    }


PHASE_LABELS = {
    "cookies": "phase_cookies",
    "claims": "phase_claims",
    "video": "phase_video",
    "probe": "phase_probe",
    "youtube": "phase_youtube",
    "content_id": "phase_content_id",
    "download": "phase_download",
    "convert": "phase_convert",
    "upload": "phase_upload",
    "audio": "phase_audio",
}


def phase_label(phase):
    return t(PHASE_LABELS[phase]) if phase in PHASE_LABELS else ""


def status_text(state, fields):
    if state["paused"]:
        return t("status_paused")
    if state.get("phase") == "done":
        count = fields["suggestions"]
        if not count:
            return t("status_done_none")
        return t("status_done_one" if count == 1 else "status_done_many", count=count)
    if state.get("phase") == "content_id":
        return t("phase_content_id", count=state["claims"], elapsed=fields["since_upload"])
    phase = phase_label(state.get("phase"))
    if state.get("phase") in STEP_PHASES:
        percent = state.get("step_percent")
        parts = (phase, None if percent is None else f"{percent:.0f}%")
        text = " ".join(part for part in parts if part)
        detail = state.get("step_detail")
        return f"{text} · {detail}" if detail else text
    if phase:
        return phase
    if fields["backoff"]:
        return f"rate limit - backoff {fields['backoff']}s"
    return t("status_sample", sample=fields["sample"])


def initial_state(now):
    return {
        "gap": None, "sample": 0, "total": 0, "sample_started": now,
        "backoff": False, "backoff_until": None, "suggestions": 0, "began": now,
        "phase": "cookies", "paused": False, "ended": None,
    }


def run_app(video_id, duration, label, own=False, waiting=False, job=None, shown_id=None):
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
            ("p", "pause", t("key_pause")),
            ("r", "refresh", t("key_refresh")),
            ("n", "new", t("key_new")),
            ("x", "clear", t("key_clear")),
            ("c", "copy", t("key_copy")),
            ("q", "quit", t("key_quit")),
            (t("yes"), "confirm", t("key_confirm")),
            ("escape", "cancel", t("key_cancel")),
        ]

        def __init__(self, video_id, duration, label, own, waiting, job, shown_id):
            super().__init__()
            self.video_id = video_id
            self.shown_id = shown_id or video_id
            self.result_id = video_id
            self.job = job
            self.stop = threading.Event()
            self.was_waiting = waiting
            self.steps_running = False
            self.steps_pending = job is not None
            self.own = own
            self.waiting = waiting
            self.waited = False
            self.notified = False
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
                        yield Input(placeholder=t("input_placeholder"), id="video-input")
                    yield RichLog(id="log")
            yield Footer()

        def on_mount(self):
            self.query_one("#tracklist", VerticalScroll).border_title = "Tracklist"
            self.query_one("#status", Vertical).border_title = t("panel_status")
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
                self.fail(t("unexpected_error", failure=failure))
                return
            if self.error is not None:
                self.fail(self.error)
                return
            if not self.gaps:
                self.notify_if_waited()
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
            if self.job is not None:
                await self.run_job()
                if self.steps_pending:
                    return
            if self.waiting:
                await self.wait_for_content_id()
            self.state["phase"] = "claims"
            self.log_line(t("log_querying"))
            self.session, self.entries, failure = await asyncio.to_thread(
                tracklist.try_claims, self.video_id, self.own
            )
            if failure:
                self.log_line(f"[!] {failure}")
            if self.typed and self.session is None:
                self.log_line(t("log_foreign_hint"))
            self.on_progress(("claims", len(self.entries)))
            self.state["phase"] = "video" if self.entries else "probe"
            if self.duration is None:
                await self.resolve_duration()
            if self.duration is None:
                self.error = t("duration_unknown")
                return
            if self.entries:
                self.gaps = tracklist.find_gaps(self.entries, self.duration)
            else:
                self.gaps = tracklist.scan_gaps(self.duration)
            missing = sum(end - start for start, end in self.gaps)
            self.log_line(t("log_loaded", duration=tracklist.hhmmss(self.duration),
                            gaps=len(self.gaps), missing=tracklist.hhmmss(missing)))
            self.state["phase"] = "recognizing"
            self.loaded = True

        async def run_job(self):
            def forward(event):
                self.call_from_thread(self.on_progress, event)

            self.steps_running = True
            try:
                outcome = await asyncio.to_thread(upload.run_steps, self.job, forward, self.stop)
            except upload.Cancelled:
                return
            finally:
                self.steps_running = False
            self.job = None
            self.steps_pending = False
            self.video_id = outcome.video_id
            self.shown_id = self.shown_id or outcome.video_id
            self.result_id = outcome.video_id
            self.own = outcome.own
            self.duration = self.duration or outcome.duration
            self.waiting = await asyncio.to_thread(upload.needs_wait, self.video_id)
            self.was_waiting = self.was_waiting or self.waiting

        async def wait_for_content_id(self):
            self.state["phase"] = "youtube"
            client = await asyncio.to_thread(upload.youtube_client)
            while True:
                status, uploaded_at = await asyncio.to_thread(
                    upload.processing_details, client, self.video_id
                )
                if status == "succeeded":
                    break
                if status != "processing":
                    raise SystemExit(t("processing_failed", video_id=self.video_id, status=status))
                self.log_line(t("processing", status=status))
                await asyncio.sleep(upload.PROCESSING_POLL)
            counts = []
            while True:
                _, self.entries, failure = await asyncio.to_thread(
                    tracklist.try_claims, self.video_id, True
                )
                if failure:
                    raise SystemExit(t("claims_unreadable", failure=failure, video_id=self.video_id))
                counts.append(len(self.entries))
                elapsed = upload.claims_elapsed(uploaded_at, time.time())
                self.state.update(phase="content_id", claims=len(self.entries), uploaded_at=uploaded_at)
                self.log_line(t("claims_count", count=len(self.entries), elapsed=tracklist.hhmmss(elapsed)))
                if elapsed >= upload.CLAIMS_TIMEOUT:
                    self.log_line(t("content_id_timeout"))
                if upload.claims_settled(counts, elapsed):
                    break
                await asyncio.sleep(upload.CLAIMS_POLL)
            await asyncio.to_thread(tracklist.mark_content_id_done, self.video_id)
            self.waiting = False
            self.waited = True

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
            self.notify_if_waited()

        def notify_if_waited(self):
            if self.waited and not self.notified:
                self.notified = True
                tracklist.notify_ready(self.title_line)

        def on_progress(self, event):
            kind = event[0]
            if kind == "step":
                _, name, percent, detail = event
                if self.state.get("phase") != name:
                    self.log_line(phase_label(name))
                self.state.update(phase=name, step_percent=percent, step_detail=detail)
            elif kind == "step-done":
                self.log_line(t("uploaded", video_id=event[2]))
            elif kind == "warning":
                self.log_line(event[1])
            elif kind == "gap":
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
                t("status_panel", gap=fields["gap"], status=estado,
                  suggestions=fields["suggestions"], elapsed=fields["elapsed"])
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
                self.shown_id, self.label, self.title_line,
            )
            log = self.query_one("#log", RichLog)
            try:
                subprocess.run(["pbcopy"], input=text.encode(), check=True)
                log.write(t("log_copied"))
            except (OSError, subprocess.CalledProcessError):
                log.write(t("log_copy_failed"))

        def action_pause(self):
            if self.pending is not None:
                return
            log = self.query_one("#log", RichLog)
            if self.state["paused"]:
                self.state["paused"] = False
                self.resume.set()
                log.write(t("log_resumed"))
            else:
                self.state["paused"] = True
                self.resume.clear()
                log.write(t("log_paused"))

        def busy_with_steps(self):
            if self.steps_running:
                self.log_line(t("log_busy_steps"))
            return self.steps_running

        def action_refresh(self):
            if self.pending is not None or self.busy_with_steps():
                return
            self.restart()

        def action_new(self):
            if self.pending is not None or self.busy_with_steps():
                return
            self.ask_or_run("new")

        def action_clear(self):
            if self.pending is not None or self.busy_with_steps():
                return
            self.ask_or_run("clear")

        def ask_or_run(self, pending):
            if self.suggestions or self.rows_state["samples"]:
                self.pending = pending
                count = len(self.suggestions) + len(tracklist.merge_samples(self.rows_state["samples"]))
                self.query_one("#log", RichLog).write(t("log_discard", count=count, yes=t("yes")))
                return
            self.apply_pending(pending)

        async def action_quit(self):
            if not self.steps_running:
                self.exit()
                return
            if self.state.get("phase") != "upload":
                await self.stop_steps_and_exit()
                return
            if self.pending != "quit":
                self.pending = "quit"
                self.log_line(t("log_quit_upload", yes=t("yes")))

        async def stop_steps_and_exit(self):
            self.stop.set()
            if self.steps_running:
                self.log_line(t("log_stopping"))
            while self.steps_running:
                await asyncio.sleep(0.05)
            self.exit()

        async def action_confirm(self):
            if self.pending is None:
                return
            pending, self.pending = self.pending, None
            if pending == "quit":
                await self.stop_steps_and_exit()
                return
            self.apply_pending(pending)

        def action_cancel(self):
            if self.pending is None:
                return
            self.pending = None
            self.query_one("#log", RichLog).write(t("log_cancelled"))

        def apply_pending(self, pending):
            if pending == "new":
                self.query_one("#log", RichLog).write(t("log_type_url"))
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
            self.shown_id = new_id
            self.own = False
            self.waiting = False
            self.typed = True
            self.entries = []
            self.gaps = []
            self.title_line = None
            self.duration = None
            self.restart()

    app = RecognizeApp(video_id, duration, label, own, waiting, job, shown_id)
    app.run()
    app.close_active()
    interrupted = app.error is None and (
        app.steps_pending or (app.was_waiting and not app.waited)
    )
    return Result(app.suggestions, app.entries, app.gaps, app.duration, app.title_line,
                  app.error, interrupted, app.result_id)
