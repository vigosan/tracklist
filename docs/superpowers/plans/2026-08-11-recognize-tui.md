# TUI en vivo para el reconocimiento — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durante `--recognize`, el terminal muestra el tracklist completo actualizándose en vivo (rich), con fallback de logs pentest sin TTY y oferta de copiar el resultado al portapapeles.

**Architecture:** `recognize_gaps` gana un callback `progress` que emite eventos puros. Un fichero nuevo `recognize_ui.py` (importado solo en el camino `--recognize`) contiene dos presentadores sobre ese flujo: `pentest_line` (puro, testeado) para stderr sin TTY, y un TUI rich cuyo estado→filas es la función pura testeada `tui_rows`. `main()` elige presentador según `sys.stderr.isatty()` y al final ofrece `pbcopy`.

**Tech Stack:** Python stdlib + `rich` (pip, solo `.venv`). Tests con `unittest`.

## Global Constraints

- El flujo sin `--recognize` sigue siendo stdlib puro y corre con el python del sistema; `recognize_ui` se importa solo dentro del camino `--recognize`, y `rich` solo dentro del presentador TUI.
- `rich` se instala únicamente en el venv: `.venv/bin/pip install rich`.
- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` desde la raíz del repo (deben pasar con el python del sistema: `recognize_ui` a nivel de módulo solo importa stdlib y `tracklist`).
- Eventos exactos del spec: `("gap", indice_desde_1, total, start, end)`, `("sample", offset, match|None)`, `("backoff",)`, `("done", n_sugerencias, segundos_enteros)`.
- Formato pentest exacto (sin colores ANSI): `[*] hueco 1/7 (00:00-04:02)`, `[+] 01:15 Artista - Tema`, `[-] 02:15 sin match`, `[!] rate limit - backoff 30s`, `[*] 8 sugerencias en 09:12`.
- stdout intocable: el tracklist final se imprime igual que hoy; todo lo demás va por stderr.
- Spec de referencia: `docs/superpowers/specs/2026-08-11-recognize-tui-design.md`.

---

### Task 1: `recognize_ui.pentest_line` — eventos a líneas de log

**Files:**
- Create: `recognize_ui.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `tracklist.hhmmss` (existente).
- Produces: módulo `recognize_ui` con `pentest_line(event: tuple) -> str | None`. Task 3 lo consume.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py` (con `import recognize_ui` junto a `import tracklist`):

```python
class PentestLineTest(unittest.TestCase):
    def test_formats_each_event_kind(self):
        self.assertEqual(recognize_ui.pentest_line(("gap", 1, 7, 0, 242)), "[*] hueco 1/7 (00:00-04:02)")
        self.assertEqual(
            recognize_ui.pentest_line(("sample", 75, {"artist": "Duran Duran", "title": "Too Late Marlene"})),
            "[+] 01:15 Duran Duran - Too Late Marlene",
        )
        self.assertEqual(recognize_ui.pentest_line(("sample", 135, None)), "[-] 02:15 sin match")
        self.assertEqual(recognize_ui.pentest_line(("backoff",)), "[!] rate limit - backoff 30s")
        self.assertEqual(recognize_ui.pentest_line(("done", 8, 552)), "[*] 8 sugerencias en 09:12")
```

Los valores fijan el formato completo: 242 s → `04:02`, 552 s → `09:12` prueban que se reutiliza `hhmmss`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'recognize_ui'`

- [ ] **Step 3: Write minimal implementation**

Crear `recognize_ui.py`:

```python
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
```

(`contextlib` y `subprocess` quedan importados para las Tasks 2-3, que completan este módulo; si el linter de la tarea se queja de imports sin uso, déjalos — se usan dos commits más adelante en este mismo plan.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add recognize_ui.py test_tracklist.py
git commit -m "Format recognition events as pentest log lines"
```

---

### Task 2: `recognize_ui.tui_rows` — estado del reconocimiento a filas del TUI

**Files:**
- Modify: `recognize_ui.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `tracklist.hhmmss`, `tracklist.find_gaps` (existentes).
- Produces: `tui_rows(entries, gaps, completed, active) -> list[str]` (strings con markup rich), `entry_line(entry) -> str`, `gap_residuals(start, end, suggestions) -> list[tuple[int, int]]`. Estado: `completed` es `dict[int, list[dict]]` (índice de hueco desde 0 → sugerencias fusionadas), `active` es `None` o `{"index": int, "sample": int, "total": int, "matches": list[dict], "backoff": bool}`. Task 3 los consume.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
ENTRY = {"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}
SUGGESTION = {"artist": "B", "title": "U", "label": "", "start": 75, "duration": 10, "suggested": True}
GAPS = [(60, 300), (400, 600)]


class TuiRowsTest(unittest.TestCase):
    def test_initial_state_shows_entries_and_pending_gaps(self):
        self.assertEqual(
            recognize_ui.tui_rows([ENTRY], GAPS, {}, None),
            [
                "[green]  - 00:00 A - T[/green]",
                "[dim]  - 01:00 ????? (sin identificar, hasta 05:00)[/dim]",
                "[dim]  - 06:40 ????? (sin identificar, hasta 10:00)[/dim]",
            ],
        )

    def test_active_gap_shows_progress_and_partial_matches(self):
        active = {"index": 0, "sample": 2, "total": 4, "matches": [SUGGESTION], "backoff": False}
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {}, active)
        self.assertEqual(rows[1], "[cyan]  - 01:00 buscando (muestra 2/4)...[/cyan]")
        self.assertEqual(rows[2], "[yellow]  - 01:15 B - U (?)[/yellow]")
        self.assertEqual(rows[3], "[dim]  - 06:40 ????? (sin identificar, hasta 10:00)[/dim]")

    def test_active_gap_before_first_sample_shows_download(self):
        active = {"index": 0, "sample": 0, "total": 4, "matches": [], "backoff": False}
        self.assertEqual(
            recognize_ui.tui_rows([ENTRY], GAPS, {}, active)[1],
            "[cyan]  - 01:00 descargando...[/cyan]",
        )

    def test_completed_gap_consolidates_suggestions_and_residuals(self):
        rows = recognize_ui.tui_rows([ENTRY], GAPS, {0: [SUGGESTION]}, None)
        self.assertEqual(rows[1], "[yellow]  - 01:15 B - U (?)[/yellow]")
        self.assertEqual(rows[2], "[dim]  - 01:25 ????? (sin identificar, hasta 05:00)[/dim]")
```

El último test protege el recálculo de residuos: la sugerencia 75-85 dentro del hueco (60, 300) deja el tramo 85-300 como `?????`, y el tramo 60-75 (15 s < 90) desaparece, igual que hará el `render` final.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'recognize_ui' has no attribute 'tui_rows'`

- [ ] **Step 3: Write minimal implementation**

Añadir a `recognize_ui.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add recognize_ui.py test_tracklist.py
git commit -m "Build live TUI rows from recognition state"
```

---

### Task 3: eventos en `recognize_gaps`, presentadores y portapapeles

**Files:**
- Modify: `tracklist.py` (`recognize_gaps` y `main()`)
- Modify: `recognize_ui.py` (`presenter`, `offer_clipboard`)

**Interfaces:**
- Consumes: `pentest_line` (Task 1), `tui_rows` y su forma de estado (Task 2), `tracklist.plan_samples`, `tracklist.merge_samples`, `tracklist.render`.
- Produces: `recognize_gaps(video_id, gaps, progress)` (tercer parámetro obligatorio), `recognize_ui.presenter(entries, gaps)` (context manager que produce el callable `progress`), `recognize_ui.offer_clipboard(text)`.

- [ ] **Step 1: Install rich in the project venv**

Run: `.venv/bin/pip install rich && .venv/bin/python3 -c "import rich; print('rich OK')"`
Expected: `rich OK`. Si falla en Python 3.14: STOP, reportar BLOCKED con el error (rich es python puro; no debería fallar).

- [ ] **Step 2: Emit events from recognize_gaps**

En `tracklist.py`, la firma pasa a `def recognize_gaps(video_id, gaps, progress):`. Dentro de `run()`:

- Al inicio: `began = time.monotonic()` (primera línea de `run()`).
- Sustituir el bloque del print actual:

```python
            for index, (start, end) in enumerate(gaps, 1):
                print(
                    f"Reconociendo hueco {index}/{len(gaps)} ({hhmmss(start)}-{hhmmss(end)})...",
                    file=sys.stderr,
                )
                source = download_gap(video_id, start, end, directory)
```

por:

```python
            for index, (start, end) in enumerate(gaps, 1):
                progress(("gap", index, len(gaps), start, end))
                source = download_gap(video_id, start, end, directory)
```

- En el guard del rate limit, añadir `progress(("backoff",))` como primera línea del `except FailedDecodeJson:` externo (antes del `await asyncio.sleep(30)`).
- Tras `samples.append((offset, match))`, añadir `progress(("sample", offset, match))`.
- Tras el bucle de huecos (fuera del `with`, antes de `return suggestions`): `progress(("done", len(suggestions), int(time.monotonic() - began)))`.

- [ ] **Step 3: Add presenter and offer_clipboard to recognize_ui**

Añadir a `recognize_ui.py`:

```python
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
```

- [ ] **Step 4: Wire the presenter in main()**

En `tracklist.py`, sustituir el final de `main()`:

```python
    gaps = find_gaps(entries, duration)
    if args.recognize and gaps:
        entries = sorted(entries + recognize_gaps(video_id, gaps), key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
    print(render(entries, gaps, video_id, args.label, title))
```

por:

```python
    gaps = find_gaps(entries, duration)
    if args.recognize and gaps:
        import recognize_ui

        with recognize_ui.presenter(entries, gaps) as progress:
            entries = sorted(entries + recognize_gaps(video_id, gaps, progress), key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, video_id, args.label, title)
        print(text)
        recognize_ui.offer_clipboard(text)
        return
    print(render(entries, gaps, video_id, args.label, title))
```

- [ ] **Step 5: Run unit tests and base-flow regression**

Run: `python3 -m unittest test_tracklist -v && python3 tracklist.py --from-json sample.json abc123def45 | tail -1`
Expected: PASS (14 tests) y la línea exacta `46 identificados, 9 huecos (31:21 sin identificar de 2:31:18)`.

- [ ] **Step 6: Smoke both presenters with fake events (no network)**

Run (stderr sin TTY → camino pentest):

```bash
.venv/bin/python3 -c "
import recognize_ui
with recognize_ui.presenter([], [(0, 242)]) as progress:
    progress(('gap', 1, 1, 0, 242))
    progress(('sample', 15, {'artist': 'X', 'title': 'Y'}))
    progress(('sample', 75, None))
    progress(('done', 1, 5))
" 2>&1
```

Expected exacto:

```
[*] hueco 1/1 (00:00-04:02)
[+] 00:15 X - Y
[-] 01:15 sin match
[*] 1 sugerencias en 00:05
```

Run (pty falso → camino TUI):

```bash
printf '' | script -q /dev/null .venv/bin/python3 -c "
import recognize_ui
with recognize_ui.presenter([], [(0, 242)]) as progress:
    progress(('gap', 1, 1, 0, 242))
    progress(('sample', 15, {'artist': 'X', 'title': 'Y'}))
    progress(('done', 1, 5))
"
```

Expected: salida con secuencias ANSI de rich conteniendo `X - Y (?)` y sin traceback; exit 0.

- [ ] **Step 7: Live end-to-end check**

Run: `echo n | script -q /dev/null .venv/bin/python3 tracklist.py --recognize _6r0Kd41jfs`
Expected: tarda varios minutos (timeout 10 min). Sin traceback; el TUI se dibuja (frames con líneas verdes/grises visibles en la captura de `script`); al final se imprime el tracklist con líneas ` (?)` y el resumen `N identificados, S sugeridos, K huecos (...)`. El prompt del portapapeles no bloquea (stdin es pipe). Guardar las últimas ~40 líneas de salida en el report.

- [ ] **Step 8: Commit**

```bash
git add tracklist.py recognize_ui.py
git commit -m "Stream recognition progress through live TUI with clipboard offer"
```
