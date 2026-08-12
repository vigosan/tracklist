# TUI elaborado con Textual — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sustituir el TUI plano de rich por una app de Textual con paneles (tracklist navegable, estado con progreso y cronómetro, log, footer de atajos) que se repinta 10 veces por segundo.

**Architecture:** `recognize_tui.py` (nuevo) contiene la app de Textual y la función pura `status_fields`. La app arranca `recognize_gaps` como worker de Textual y consume los eventos existentes por el callback `progress`; `recognize_gaps` no cambia. `recognize_ui.py` conserva sus funciones puras y `offer_clipboard`, y su `presenter` pasa a delegar en Textual cuando hay TTY.

**Tech Stack:** Python stdlib + `textual` 8.2.8 (ya instalado en `.venv`, verificado en Python 3.14) + `unittest`.

## Global Constraints

- El flujo sin reconocimiento sigue siendo stdlib puro con el python del sistema; `recognize_tui` se importa solo dentro del camino con TTY.
- Sin TTY: modo pentest actual, byte a byte igual.
- Con TTY pero sin `textual` importable: aviso por stderr y degradación a pentest, NUNCA abortar.
- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` desde la raíz, con el python del sistema (17 pasan ahora; `recognize_tui` a nivel de módulo solo importa stdlib, para que el test de `status_fields` corra sin textual).
- `recognize_gaps` y el formato de eventos NO se tocan.
- stdout sigue llevando solo el tracklist.
- Spec de referencia: `docs/superpowers/specs/2026-08-12-textual-tui-design.md`.

---

### Task 1: `status_fields` — estado del panel derecho

**Files:**
- Create: `recognize_tui.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `tracklist.hhmmss`.
- Produces: módulo `recognize_tui` con `SAMPLE_ESTIMATE = 4.0` y `status_fields(state, now) -> dict`. Task 2 lo consume.

Forma del estado (dict) que recibe `status_fields`:

```python
{"gap": (3, 7), "sample": 6, "total": 12, "sample_started": 100.0,
 "backoff": False, "suggestions": 8, "began": 50.0}
```

`gap` es `None` antes del primer hueco. Salida:
`{"gap": "3/7", "percent": int, "sample": "7/12", "suggestions": int, "elapsed": "04:31", "backoff": bool}`.

Reglas del porcentaje: `fraccion = 0.0` si `backoff`, si no
`min(0.95, (now - sample_started) / SAMPLE_ESTIMATE)`;
`percent = int(100 * (sample + fraccion) / total)`, y `0` si `total` es 0.
`sample` de salida es la muestra en curso: `min(sample + 1, total)`.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py` (con `import recognize_tui` junto a los otros imports):

```python
STATE = {
    "gap": (3, 7),
    "sample": 6,
    "total": 12,
    "sample_started": 100.0,
    "backoff": False,
    "suggestions": 8,
    "began": 50.0,
}


class StatusFieldsTest(unittest.TestCase):
    def test_interpolates_progress_inside_the_current_sample(self):
        fields = recognize_tui.status_fields(STATE, 102.0)
        self.assertEqual(fields["gap"], "3/7")
        self.assertEqual(fields["sample"], "7/12")
        self.assertEqual(fields["percent"], 54)
        self.assertEqual(fields["suggestions"], 8)
        self.assertEqual(fields["elapsed"], "00:52")
        self.assertFalse(fields["backoff"])

    def test_progress_freezes_during_backoff(self):
        fields = recognize_tui.status_fields({**STATE, "backoff": True}, 160.0)
        self.assertEqual(fields["percent"], 50)
        self.assertTrue(fields["backoff"])

    def test_interpolation_never_reaches_the_next_sample(self):
        self.assertEqual(recognize_tui.status_fields(STATE, 999.0)["percent"], 57)

    def test_before_the_first_gap(self):
        idle = {"gap": None, "sample": 0, "total": 0, "sample_started": 0.0,
                "backoff": False, "suggestions": 0, "began": 50.0}
        fields = recognize_tui.status_fields(idle, 50.0)
        self.assertEqual(fields["gap"], "-")
        self.assertEqual(fields["percent"], 0)
        self.assertEqual(fields["sample"], "-")
        self.assertEqual(fields["elapsed"], "00:00")
```

Los números fijan las reglas: a 2 s de una muestra de 4 s la fracción es 0.5
→ `(6 + 0.5) / 12 = 54%`; con backoff cae a `6/12 = 50%`; pasado mucho
tiempo se topa en 0.95 → `(6 + 0.95) / 12 = 57%`, nunca 58 (que sería la
muestra siguiente ya confirmada).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'recognize_tui'`

- [ ] **Step 3: Write minimal implementation**

Crear `recognize_tui.py`:

```python
import tracklist

SAMPLE_ESTIMATE = 4.0
SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"


def status_fields(state, now):
    total = state["total"]
    if state["backoff"]:
        fraction = 0.0
    else:
        fraction = min(0.95, (now - state["sample_started"]) / SAMPLE_ESTIMATE)
    percent = int(100 * (state["sample"] + fraction) / total) if total else 0
    gap = state["gap"]
    return {
        "gap": f"{gap[0]}/{gap[1]}" if gap else "-",
        "percent": percent,
        "sample": f"{min(state['sample'] + 1, total)}/{total}" if total else "-",
        "suggestions": state["suggestions"],
        "elapsed": tracklist.hhmmss(int(now - state["began"])),
        "backoff": state["backoff"],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
git add recognize_tui.py test_tracklist.py
git commit -m "Compute recognition status fields for the TUI panel"
```

---

### Task 2: la app de Textual

**Files:**
- Modify: `recognize_tui.py`

**Interfaces:**
- Consumes: `status_fields`, `SPINNER` (Task 1); `recognize_ui.tui_rows`, `recognize_ui.pentest_line`; `tracklist.recognize_gaps`, `tracklist.merge_samples`, `tracklist.plan_samples`, `tracklist.render`.
- Produces: `run_app(video_id, entries, gaps, duration, label, title) -> list[dict]` — ejecuta la app y devuelve las sugerencias acumuladas. Task 3 la consume.

- [ ] **Step 1: Write implementation**

Es una app interactiva; su verificación es manual (pasos siguientes). Añadir a `recognize_tui.py`, tras `status_fields`:

```python
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Label, ProgressBar, RichLog, Static

import recognize_ui


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
            yield Static(id="tracklist")
            with Vertical(id="side"):
                with Vertical(id="status"):
                    yield Label(id="status-text")
                    yield ProgressBar(total=100, show_eta=False, id="bar")
                yield RichLog(id="log", markup=True)
        yield Footer()

    def on_mount(self):
        self.query_one("#tracklist", Static).border_title = "Tracklist"
        self.query_one("#status", Vertical).border_title = "Estado"
        self.query_one("#log", RichLog).border_title = "Log"
        self.set_interval(0.1, self.refresh_view)
        self.run_worker(self.recognize(), exclusive=True)

    async def recognize(self):
        self.suggestions = await tracklist.recognize_gaps_async(
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
        self.query_one("#tracklist", Static).update("\n".join(rows))
        estado = "rate limit - backoff" if fields["backoff"] else f"Muestra {fields['sample']}"
        self.query_one("#status-text", Label).update(
            f"Hueco {fields['gap']}   {estado}\n"
            f"Sugerencias {fields['suggestions']}   Tiempo {fields['elapsed']}"
        )
        self.query_one("#bar", ProgressBar).update(progress=fields["percent"])

    def action_copy(self):
        entries = sorted(self.entries + self.suggestions, key=lambda entry: entry["start"])
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


def run_app(video_id, entries, gaps, duration, label, title):
    app = RecognizeApp(video_id, entries, gaps, duration, label, title)
    app.run()
    return app.suggestions
```

Y añadir `import subprocess` junto a `import time` al inicio del módulo (`tracklist` ya está importado desde Task 1; los imports de textual y `recognize_ui` van también arriba, después de los de stdlib).

- [ ] **Step 2: Add the async entry point to recognize_gaps**

`recognize_gaps` usa `asyncio.run`, que no puede anidarse dentro del bucle de Textual, así que hace falta una variante que devuelva la corrutina. Es un cambio de dos líneas, sin tocar la lógica.

Hoy `tracklist.py:278` es `def recognize_gaps(video_id, gaps, progress):`, seguido del `try/except ImportError` del import perezoso, luego `async def run():` con todo el cuerpo, y al final `return asyncio.run(run())`.

Cambio 1 — renombrar la función externa. Sustituir la línea 278:

```python
def recognize_gaps(video_id, gaps, progress):
```

por:

```python
def recognize_gaps_async(video_id, gaps, progress):
```

Cambio 2 — la última línea de esa función, `    return asyncio.run(run())`, pasa a devolver la corrutina sin ejecutarla, y se añade el envoltorio síncrono justo después de la función. Sustituir:

```python
    return asyncio.run(run())
```

por:

```python
    return run()


def recognize_gaps(video_id, gaps, progress):
    return asyncio.run(recognize_gaps_async(video_id, gaps, progress))
```

Resultado: `recognize_gaps(video_id, gaps, progress)` mantiene exactamente la firma y el comportamiento de hoy (lo usa el camino pentest), y `recognize_gaps_async(...)` devuelve una corrutina que Textual puede await-ear. El `sys.exit` del import perezoso sigue ejecutándose al llamar, antes de crear la corrutina, en ambos caminos.

- [ ] **Step 3: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (21 tests)

- [ ] **Step 4: Verify the non-recognition flow is untouched**

Run: `python3 tracklist.py --from-json sample.json --no-recognize _6r0Kd41jfs | tail -1`
Expected: `46 identificados, 9 huecos (31:21 sin identificar de 2:31:18)`

- [ ] **Step 5: Smoke the app with fake events (no network)**

Run:

```bash
.venv/bin/python3 -c "
import asyncio, recognize_tui, tracklist

async def fake(video_id, gaps, progress):
    progress(('gap', 1, 1, 0, 242))
    await asyncio.sleep(0.5)
    progress(('sample', 15, {'artist': 'X', 'title': 'Y'}))
    await asyncio.sleep(0.5)
    progress(('done', 1, 1))
    return []

tracklist.recognize_gaps_async = fake
entries = [{'artist': 'A', 'title': 'T', 'label': '', 'start': 250, 'duration': 60}]
print('SUGERENCIAS:', recognize_tui.run_app('vid12345678', entries, [(0, 242)], 400, False, None))
" < /dev/null
```

Expected: la app arranca y se cierra sola en ~1 s; imprime `SUGERENCIAS:` con una lista (vacía o con la sugerencia de X - Y); sin traceback; exit 0. Si Textual se queja por no tener terminal, ejecutar el mismo comando bajo `script -q /dev/null` y anotarlo en el report.

- [ ] **Step 6: Commit**

```bash
git add recognize_tui.py tracklist.py
git commit -m "Add Textual application for live recognition"
```

---

### Task 3: cableado, degradación y verificación en vivo

**Files:**
- Modify: `recognize_ui.py` (`presenter` deja de usar rich)
- Modify: `tracklist.py` (`main()` usa el TUI cuando hay TTY)

**Interfaces:**
- Consumes: `recognize_tui.run_app` (Task 2), `recognize_ui._pentest_progress`.
- Produces: `recognize_ui.textual_available() -> bool`; `main()` con dos caminos (TUI / pentest).

- [ ] **Step 1: Replace the rich presenter**

En `recognize_ui.py`, borrar la función `presenter` entera (el `@contextlib.contextmanager` y su cuerpo, líneas que van desde `@contextlib.contextmanager` hasta el `yield progress` final) y sustituirla por:

```python
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
```

Si tras borrar `presenter` el import `contextlib` queda sin uso en el módulo, eliminarlo también.

- [ ] **Step 2: Wire main()**

En `tracklist.py`, sustituir el bloque:

```python
    if args.recognize and gaps and recognition_available():
        import recognize_ui

        with recognize_ui.presenter(entries, gaps) as progress:
            entries = sorted(entries + recognize_gaps(video_id, gaps, progress), key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, video_id, args.label, title)
        print(text)
        recognize_ui.offer_clipboard(text)
        return
```

por:

```python
    if args.recognize and gaps and recognition_available():
        import recognize_ui

        if recognize_ui.textual_available():
            import recognize_tui

            found = recognize_tui.run_app(video_id, entries, gaps, duration, args.label, title)
        else:
            found = recognize_gaps(video_id, gaps, recognize_ui._pentest_progress)
        entries = sorted(entries + found, key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, video_id, args.label, title)
        print(text)
        recognize_ui.offer_clipboard(text)
        return
```

- [ ] **Step 3: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (21 tests)

- [ ] **Step 4: Verify pentest fallback still works (no TTY)**

Run: `.venv/bin/python3 tracklist.py --from-json sample.json --duration 9000 _6r0Kd41jfs 2>&1 >/dev/null | head -3`
Expected: líneas `[*] hueco 1/...` por stderr (camino pentest, porque no hay TTY). Cancelar con Ctrl-C tras ver las primeras líneas si tarda; anotar en el report que se interrumpió a propósito.

- [ ] **Step 5: Live end-to-end check**

Run: `script -q /dev/null .venv/bin/python3 tracklist.py _6r0Kd41jfs < /dev/null > /tmp/tui_live.txt 2>&1; echo "EXIT=$?"`
Expected: timeout 15 minutos. `EXIT=0`; el fichero contiene frames del TUI (secuencias ANSI con `Tracklist`, `Estado`, `Log`) y, al final, el tracklist con líneas ` (?)` y el resumen `N identificados, S sugeridos, K huecos (...)`. Guardar en el report las últimas 20 líneas con `sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'` aplicado.

- [ ] **Step 6: Commit**

```bash
git add recognize_ui.py tracklist.py
git commit -m "Show the Textual TUI when running on a terminal"
```
