# Arranque instantáneo y controles del TUI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** El TUI aparece al instante, no se congela durante las descargas, y ofrece pausar (`p`), cambiar de vídeo (`n`) y vaciar (`x`).

**Architecture:** Cuatro cambios independientes. (1) `recognize_gaps_async` acepta una pausa opcional y mueve sus llamadas bloqueantes a hilos. (2) `recognize_tui` gana dos funciones puras (`phase_label`, `initial_state`). (3) La app pasa a cargar los datos ella misma dentro de un worker, con fases visibles, y `main()` deja de hacer red antes del TUI. (4) Las tres teclas nuevas sobre esa base.

**Tech Stack:** Python stdlib (`asyncio`), textual 8.2.8, unittest.

## Global Constraints

- `recognize_tui.py` debe seguir importable con el python del sistema (sin textual): los imports de textual y la clase viven dentro de `run_app`.
- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` con el python del sistema (22 pasan ahora).
- El camino sin TTY (pentest) no cambia en absoluto, ni el formato de eventos.
- stdout sigue llevando solo el tracklist.
- Spec de referencia: `docs/superpowers/specs/2026-08-12-instant-tui-controls-design.md`.

---

### Task 1: descargas en hilo y pausa en el motor

**Files:**
- Modify: `tracklist.py` (`recognize_gaps_async`, `recognize_gaps`)

**Interfaces:**
- Consumes: nada nuevo.
- Produces: `recognize_gaps_async(video_id, gaps, progress, resume=None)` — `resume` es `None` o un objeto con `await resume.wait()` (un `asyncio.Event`); `recognize_gaps(video_id, gaps, progress)` sin cambios de firma. Tasks 3-4 los consumen.

- [ ] **Step 1: Write implementation**

Es un cambio en código de red/subprocess, sin unit test propio (se verifica por el camino pentest en el Step 3 y en vivo en la Task 4).

En `tracklist.py`, cambiar la firma:

```python
def recognize_gaps_async(video_id, gaps, progress):
```

por:

```python
def recognize_gaps_async(video_id, gaps, progress, resume=None):
```

Dentro de `run()`, sustituir la línea de descarga:

```python
                source = download_gap(video_id, start, end, directory)
```

por:

```python
                source = await asyncio.to_thread(download_gap, video_id, start, end, directory)
```

Sustituir la línea del corte de muestra:

```python
                    sample = cut_sample(source, offset - start, directory)
```

por:

```python
                    if resume is not None:
                        await resume.wait()
                    sample = await asyncio.to_thread(cut_sample, source, offset - start, directory)
```

`recognize_gaps` no cambia: sigue siendo `return asyncio.run(recognize_gaps_async(video_id, gaps, progress))`, y al no pasar `resume` el comportamiento es idéntico al de hoy.

- [ ] **Step 2: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (22 tests)

- [ ] **Step 3: Verify the pentest path still recognizes**

Run: `.venv/bin/python3 tracklist.py --from-json sample.json --duration 600 _6r0Kd41jfs 2>&1 >/dev/null | head -4`
Expected: líneas `[*] hueco 1/...` y al menos una `[+]` o `[-]` — confirma que descarga y corte siguen funcionando a través de `to_thread`. Interrumpir tras ver las primeras líneas (timeout ~120 s) y anotarlo en el report.

- [ ] **Step 4: Commit**

```bash
git add tracklist.py
git commit -m "Run downloads off the event loop and support pausing"
```

---

### Task 2: `phase_label` e `initial_state`

**Files:**
- Modify: `recognize_tui.py` (dos funciones a nivel de módulo, tras `status_fields`)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: nada.
- Produces: `phase_label(phase) -> str`, `initial_state(now) -> dict`. Tasks 3-4 los consumen.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class PhaseAndStateTest(unittest.TestCase):
    def test_phase_labels(self):
        self.assertEqual(recognize_tui.phase_label("cookies"), "Leyendo cookies de Brave...")
        self.assertEqual(recognize_tui.phase_label("claims"), "Pidiendo reclamaciones a Studio...")
        self.assertEqual(recognize_tui.phase_label("video"), "Pidiendo datos del video...")
        self.assertEqual(recognize_tui.phase_label("recognizing"), "")
        self.assertEqual(recognize_tui.phase_label("cualquier-otra"), "")

    def test_initial_state_starts_empty(self):
        state = recognize_tui.initial_state(500.0)
        self.assertIsNone(state["gap"])
        self.assertEqual(state["sample"], 0)
        self.assertEqual(state["total"], 0)
        self.assertEqual(state["suggestions"], 0)
        self.assertFalse(state["backoff"])
        self.assertEqual(state["began"], 500.0)
        self.assertEqual(state["sample_started"], 500.0)
        self.assertEqual(state["phase"], "cookies")
        self.assertFalse(state["paused"])
```

`initial_state` fija el contrato del reinicio: todo a cero, fase inicial `cookies`, sin pausa.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'recognize_tui' has no attribute 'phase_label'`

- [ ] **Step 3: Write minimal implementation**

En `recognize_tui.py`, tras `status_fields`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (24 tests)

- [ ] **Step 5: Commit**

```bash
git add recognize_tui.py test_tracklist.py
git commit -m "Add phase labels and initial state for the TUI"
```

---

### Task 3: la app carga sus propios datos

**Files:**
- Modify: `recognize_tui.py` (`run_app` y la clase interna)
- Modify: `tracklist.py` (`main()`)

**Interfaces:**
- Consumes: `phase_label`, `initial_state` (Task 2); `recognize_gaps_async` (Task 1); `tracklist.fresh_session`, `fetch_claims`, `fetch_videos`, `extract`, `find_gaps`.
- Produces: `run_app(video_id, duration, label) -> (suggestions, entries, gaps, duration, title, error)` — la app devuelve todo lo que `main()` necesita para renderizar, más `error` (`None` o el mensaje con el que salir).

- [ ] **Step 1: Rewrite run_app's signature and loading**

En `recognize_tui.py`:

a) La firma pasa de `def run_app(video_id, entries, gaps, duration, label, title):` a:

```python
def run_app(video_id, duration, label):
```

b) El `__init__` de la clase pasa de recibir datos cargados a recibir solo lo mínimo:

```python
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
```

c) `on_mount` deja de lanzar `self.recognize()` y lanza el nuevo worker de carga:

```python
        def on_mount(self):
            self.query_one("#tracklist", VerticalScroll).border_title = "Tracklist"
            self.query_one("#status", Vertical).border_title = "Estado"
            self.query_one("#log", RichLog).border_title = "Log"
            self.set_interval(0.1, self.refresh_view)
            self.run_worker(self.load_and_recognize(), exclusive=True)
```

d) Añadir el worker de carga justo antes de `recognize`:

```python
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
```

e) `recognize` se simplifica a (sin `self.exit()` propio, que ahora lo hace el flujo de arriba):

```python
        async def recognize(self):
            await tracklist.recognize_gaps_async(self.video_id, self.gaps, self.on_progress)
            self.exit()
```

f) Añadir `import asyncio` al inicio del módulo (junto a `subprocess` y `time`).

g) `refresh_view` muestra la fase mientras carga. Sustituir la línea:

```python
            estado = "rate limit - backoff" if fields["backoff"] else f"Muestra {fields['sample']}"
```

por:

```python
            phase = phase_label(self.state["phase"])
            if phase:
                estado = phase
            elif fields["backoff"]:
                estado = "rate limit - backoff"
            else:
                estado = f"Muestra {fields['sample']}"
```

h) El cierre de `run_app` pasa a devolver todo:

```python
    app = RecognizeApp(video_id, duration, label)
    app.run()
    app.close_active()
    return app.suggestions, app.entries, app.gaps, app.duration, app.title_line, app.error
```

- [ ] **Step 2: Rewire main()**

En `tracklist.py`, el bloque actual va desde `    title = None` hasta el `print(render(...))` final. Sustituir TODO ese bloque:

```python
    title = None
    duration = args.duration
    if args.from_json:
        response = json.loads(Path(args.from_json).read_text())
    else:
        session = fresh_session()
        response = fetch_claims(video_id, session)
        if duration is None:
            info = fetch_videos([video_id], session).get(video_id)
            if info:
                title = info["title"]
                duration = info["length"] or None

    entries = extract(response)
    if not entries:
        sys.exit("Sin reclamaciones con timestamp para este video.")
    gaps = find_gaps(entries, duration)
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
    print(render(entries, gaps, video_id, args.label, title))
```

por:

```python
    if not args.from_json and args.recognize and recognition_available():
        import recognize_ui

        if recognize_ui.textual_available():
            import recognize_tui

            found, entries, gaps, duration, title, error = recognize_tui.run_app(
                video_id, args.duration, args.label
            )
            if error:
                sys.exit(error)
            entries = sorted(entries + found, key=lambda entry: entry["start"])
            gaps = find_gaps(entries, duration)
            text = render(entries, gaps, video_id, args.label, title)
            print(text)
            recognize_ui.offer_clipboard(text)
            return

    title = None
    duration = args.duration
    if args.from_json:
        response = json.loads(Path(args.from_json).read_text())
    else:
        session = fresh_session()
        response = fetch_claims(video_id, session)
        if duration is None:
            info = fetch_videos([video_id], session).get(video_id)
            if info:
                title = info["title"]
                duration = info["length"] or None

    entries = extract(response)
    if not entries:
        sys.exit("Sin reclamaciones con timestamp para este video.")
    gaps = find_gaps(entries, duration)
    if args.recognize and gaps and recognition_available():
        import recognize_ui

        found = recognize_gaps(video_id, gaps, recognize_ui._pentest_progress)
        entries = sorted(entries + found, key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, video_id, args.label, title)
        print(text)
        recognize_ui.offer_clipboard(text)
        return
    print(render(entries, gaps, video_id, args.label, title))
```

El camino del TUI sale antes de tocar la red; el resto (incluido `--from-json`, que nunca usa TUI) queda como estaba.

- [ ] **Step 3: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (24 tests)

- [ ] **Step 4: Verify the non-TUI paths are unchanged**

Run: `python3 tracklist.py --from-json sample.json --no-recognize _6r0Kd41jfs | tail -1`
Expected: `46 identificados, 9 huecos (31:21 sin identificar de 2:31:18)`

Run: `python3 tracklist.py --from-json sample.json _6r0Kd41jfs 2>&1 >/dev/null | head -1`
Expected: `Aviso: reconocimiento de huecos desactivado (faltan dependencias).`

- [ ] **Step 5: Measure the time to first paint**

Run:

```bash
script -q /dev/null .venv/bin/python3 -c "
import time, recognize_tui
t0 = time.monotonic()
import textual.app
print('IMPORT', round(time.monotonic()-t0, 2))
" < /dev/null | tail -1
```

Expected: menos de 1,0 s — confirma que nada bloqueante precede al primer pintado (la red ya vive dentro del worker). Anotar el valor en el report.

- [ ] **Step 6: Live end-to-end check**

Run: `script -q /dev/null .venv/bin/python3 tracklist.py _6r0Kd41jfs < /dev/null > /tmp/instant_live.txt 2>&1; echo "EXIT=$?"`
Expected: timeout 900000 ms. `EXIT=0`. Comprobar en el fichero, con `sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g'`, que aparecen las fases (`Leyendo cookies`, `Pidiendo reclamaciones`) y al final el tracklist con ` (?)` y su resumen. Guardar las últimas 15 líneas limpias en el report.

- [ ] **Step 7: Commit**

```bash
git add recognize_tui.py tracklist.py
git commit -m "Load claims inside the TUI so it paints immediately"
```

---

### Task 4: teclas de pausa, nuevo vídeo y vaciar

**Files:**
- Modify: `recognize_tui.py` (BINDINGS, acciones, worker)

**Interfaces:**
- Consumes: `initial_state` (Task 2), `recognize_gaps_async` con `resume` (Task 1), `load_and_recognize` (Task 3).
- Produces: acciones `action_pause`, `action_new`, `action_clear`.

- [ ] **Step 1: Write implementation**

a) En `__init__`, tras `self.frame = 0`, añadir:

```python
            self.resume = asyncio.Event()
            self.resume.set()
            self.pending = None
```

b) BINDINGS pasa a:

```python
        BINDINGS = [
            ("p", "pause", "Pausa"),
            ("n", "new", "Nuevo"),
            ("x", "clear", "Vaciar"),
            ("c", "copy", "Copiar"),
            ("q", "quit", "Salir"),
        ]
```

c) `recognize` pasa la pausa al motor:

```python
        async def recognize(self):
            await tracklist.recognize_gaps_async(
                self.video_id, self.gaps, self.on_progress, self.resume
            )
            self.exit()
```

d) Añadir las tres acciones y el reinicio, tras `action_copy`:

```python
        def action_pause(self):
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
            self.ask_or_run("new")

        def action_clear(self):
            self.ask_or_run("clear")

        def ask_or_run(self, pending):
            if self.suggestions:
                self.pending = pending
                self.query_one("#log", RichLog).write(
                    f"[!] descartar {len(self.suggestions)} sugerencias? s/n"
                )
                return
            self.apply_pending(pending)

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

        def on_key(self, event):
            if self.pending is None:
                return
            pending, self.pending = self.pending, None
            if event.key == "s":
                self.apply_pending(pending)
            else:
                self.query_one("#log", RichLog).write("[*] cancelado")

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
```

e) `compose` añade el campo de entrada oculto dentro del panel de estado, tras la barra:

```python
                    with Vertical(id="status"):
                        yield Label(id="status-text")
                        yield ProgressBar(total=100, show_eta=False, id="bar")
                        yield Input(placeholder="URL o ID", id="video-input")
```

y el import de widgets pasa a incluir `Input`:

```python
    from textual.widgets import Footer, Input, Label, ProgressBar, RichLog, Static
```

f) El campo arranca oculto: en `on_mount`, tras las líneas de `border_title`, añadir:

```python
            self.query_one("#video-input", Input).display = False
```

g) `refresh_view` muestra la pausa. La cadena `estado` se decide ahora así — sustituir el bloque introducido en la Task 3 por:

```python
            phase = phase_label(self.state["phase"])
            if self.state["paused"]:
                estado = "PAUSA"
            elif phase:
                estado = phase
            elif fields["backoff"]:
                estado = "rate limit - backoff"
            else:
                estado = f"Muestra {fields['sample']}"
```

- [ ] **Step 2: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (24 tests)

- [ ] **Step 3: Smoke the keys with a fake backend**

Con un backend falso lento se ejercitan pausa y vaciar sin red. Ejecutar:

```bash
.venv/bin/python3 -c "
import asyncio, recognize_tui, tracklist

async def fake_session():
    return {'headers': {}, 'context': {}}

def fake_claims(video_id, session):
    return {'receivedClaims': []}

tracklist.fresh_session = lambda: {'headers': {}, 'context': {}}
tracklist.fetch_claims = fake_claims
result = recognize_tui.run_app('vid12345678', 600, False)
print('ERROR:', result[5])
" < /dev/null 2>/dev/null | tail -1
```

Expected: `ERROR: Sin reclamaciones con timestamp para este video.` — confirma que el camino de error de carga funciona y que la app se cierra sola sin traza.

- [ ] **Step 4: Live check of the keys**

Run: `script -q /dev/null .venv/bin/python3 tracklist.py _6r0Kd41jfs`
Expected: es una comprobación interactiva; el implementador NO puede hacerla (requiere pulsar teclas). Anotar en el report que queda pendiente de verificación manual por el usuario, listando qué probar: `p` pausa y reanuda, `x` pide confirmación y reinicia, `n` pide un ID nuevo, `c` copia, `q` sale.

- [ ] **Step 5: Commit**

```bash
git add recognize_tui.py
git commit -m "Add pause, new video and clear controls to the TUI"
```
