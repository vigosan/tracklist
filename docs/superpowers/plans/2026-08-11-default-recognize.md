# Reconocimiento por defecto y prompt de vídeo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tracklist` reconoce huecos por defecto (degradando con aviso si faltan dependencias), pregunta el vídeo cuando falta el argumento y hay terminal, y acepta URLs de Studio.

**Architecture:** Tres cambios acotados en `tracklist.py`: una función pura `parse_video_id` extraída de `main()` y ampliada con el patrón de Studio; el flag `--recognize` convertido a `BooleanOptionalAction` con default `True` más un guard de disponibilidad de shazamio que degrada con aviso; y un prompt interactivo cuando falta el argumento de vídeo. `recognize_ui.py` no se toca.

**Tech Stack:** Python stdlib (`argparse`, `re`, `unittest`).

## Global Constraints

- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` desde la raíz del repo, con el python del sistema (16 tests pasan ahora).
- El aviso de degradación y el prompt van a stderr; stdout sigue llevando solo el tracklist.
- `--from-json`, `--save-session`, `--duration`, `--label` no cambian de comportamiento.
- Spec de referencia: `docs/superpowers/specs/2026-08-11-default-recognize-design.md`.

---

### Task 1: `parse_video_id` — extracción del ID con soporte de Studio

**Files:**
- Modify: `tracklist.py` (función nueva antes de `main`; `main()` la usa)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: nada.
- Produces: `parse_video_id(text: str) -> str`. Task 3 la usa desde `main()`.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class ParseVideoIdTest(unittest.TestCase):
    def test_extracts_id_from_every_supported_form(self):
        self.assertEqual(tracklist.parse_video_id("https://www.youtube.com/watch?v=_6r0Kd41jfs"), "_6r0Kd41jfs")
        self.assertEqual(tracklist.parse_video_id("https://youtu.be/_6r0Kd41jfs"), "_6r0Kd41jfs")
        self.assertEqual(tracklist.parse_video_id("https://studio.youtube.com/video/_6r0Kd41jfs/edit"), "_6r0Kd41jfs")
        self.assertEqual(tracklist.parse_video_id("_6r0Kd41jfs"), "_6r0Kd41jfs")
```

El ID `_6r0Kd41jfs` empieza por guion bajo a propósito: prueba que la clase `[\w-]` cubre los IDs reales del usuario.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'parse_video_id'`

- [ ] **Step 3: Write minimal implementation**

En `tracklist.py`, justo antes de `def main():`:

```python
def parse_video_id(text):
    match = re.search(r"(?:v=|youtu\.be/|video/)([\w-]{11})", text)
    return match.group(1) if match else text
```

Y en `main()`, sustituir estas dos líneas:

```python
    match = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", args.video)
    video_id = match.group(1) if match else args.video
```

por:

```python
    video_id = parse_video_id(args.video)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Verify the CLI still resolves ids**

Run: `python3 tracklist.py --from-json sample.json 'https://studio.youtube.com/video/_6r0Kd41jfs/edit' | head -1`
Expected: `https://www.youtube.com/watch?v=_6r0Kd41jfs`

- [ ] **Step 6: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Accept Studio URLs when parsing the video id"
```

---

### Task 2: reconocimiento activado por defecto con degradación

**Files:**
- Modify: `tracklist.py` (`main()`: definición del flag y bloque de reconocimiento)

**Interfaces:**
- Consumes: `recognize_gaps`, `recognize_ui.presenter`, `recognize_ui.offer_clipboard` (existentes).
- Produces: comportamiento por defecto `args.recognize is True`; `--no-recognize` disponible.

- [ ] **Step 1: Write implementation**

Esta tarea es cableado de CLI y degradación; no lleva unit test (verificación por CLI en los pasos siguientes).

En `main()`, sustituir la línea del flag:

```python
    parser.add_argument("--recognize", action="store_true", help="identifica los huecos con Shazam")
```

por:

```python
    parser.add_argument(
        "--recognize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="identifica los huecos con Shazam",
    )
```

Y sustituir el bloque de reconocimiento entero:

```python
    if args.recognize and gaps:
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

        with recognize_ui.presenter(entries, gaps) as progress:
            entries = sorted(entries + recognize_gaps(video_id, gaps, progress), key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
        text = render(entries, gaps, video_id, args.label, title)
        print(text)
        recognize_ui.offer_clipboard(text)
        return
```

Y añadir, justo antes de `def parse_video_id(text):`:

```python
def recognition_available():
    try:
        import shazamio  # noqa: F401
    except ImportError:
        print(
            "Aviso: reconocimiento de huecos desactivado (faltan dependencias).\n"
            "Para activarlo: .venv/bin/python3 tracklist.py <video>\n"
            "(una vez: .venv/bin/pip install audioop-lts shazamio==0.4.0.1 rich)",
            file=sys.stderr,
        )
        return False
    return True
```

(El `# noqa: F401` es la única marca permitida: no es un comentario explicativo sino una directiva de linter para un import intencionadamente sin uso.)

- [ ] **Step 2: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (17 tests)

- [ ] **Step 3: Verify degradation with the system python**

Run: `python3 tracklist.py --from-json sample.json _6r0Kd41jfs 2>/tmp/warn.txt | tail -1; cat /tmp/warn.txt`
Expected: stdout termina en `46 identificados, 9 huecos (31:21 sin identificar de 2:31:18)` (sin `sugeridos`), y stderr contiene exactamente las tres líneas del aviso, empezando por `Aviso: reconocimiento de huecos desactivado (faltan dependencias).`

- [ ] **Step 4: Verify --no-recognize exists and silences the warning**

Run: `python3 tracklist.py --from-json sample.json --no-recognize _6r0Kd41jfs 2>/tmp/warn2.txt | tail -1; wc -c </tmp/warn2.txt`
Expected: la misma última línea del tracklist y `0` bytes en stderr (con `--no-recognize` no se llega a comprobar disponibilidad).

- [ ] **Step 5: Verify the venv python still recognizes by default**

Run: `.venv/bin/python3 tracklist.py --from-json sample.json --no-recognize _6r0Kd41jfs | tail -1`
Expected: la misma última línea del tracklist, sin aviso — confirma que el venv no rompe el camino sin reconocimiento. (El camino con reconocimiento real se prueba en la Task 3.)

- [ ] **Step 6: Commit**

```bash
git add tracklist.py
git commit -m "Recognize gaps by default with graceful degradation"
```

---

### Task 3: prompt interactivo del vídeo y verificación en vivo

**Files:**
- Modify: `tracklist.py` (`main()`: bloque `if not args.video`)

**Interfaces:**
- Consumes: `parse_video_id` (Task 1), el default de `--recognize` (Task 2).
- Produces: comportamiento interactivo; sin cambios de firma.

- [ ] **Step 1: Write implementation**

En `main()`, sustituir:

```python
    if not args.video:
        parser.error("indica la URL o el ID del video")
```

por:

```python
    if not args.video and sys.stdin.isatty() and sys.stderr.isatty():
        print("URL o ID del video: ", end="", file=sys.stderr, flush=True)
        try:
            args.video = input().strip()
        except EOFError:
            print(file=sys.stderr)
    if not args.video:
        parser.error("indica la URL o el ID del video")
```

- [ ] **Step 2: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (17 tests)

- [ ] **Step 3: Verify non-interactive behaviour is unchanged**

Run: `python3 tracklist.py --from-json sample.json < /dev/null; echo "exit=$?"`
Expected: mensaje de argparse `indica la URL o el ID del video` en stderr y `exit=2`.

- [ ] **Step 4: Verify the prompt under a fake TTY**

Run:

```bash
printf 'https://studio.youtube.com/video/_6r0Kd41jfs/edit\n' | script -q /dev/null python3 tracklist.py --from-json sample.json --no-recognize | head -2
```

Expected: la salida incluye el prompt `URL o ID del video:` y después `https://www.youtube.com/watch?v=_6r0Kd41jfs` — confirma prompt + parseo de URL de Studio en el mismo camino.

- [ ] **Step 5: Live end-to-end check with defaults**

Run: `echo n | script -q /dev/null .venv/bin/python3 tracklist.py _6r0Kd41jfs`
Expected: tarda varios minutos (timeout 10 min). Sin `--recognize` explícito, el TUI se dibuja igual que antes y la salida final incluye líneas ` (?)` y el resumen `N identificados, S sugeridos, K huecos (...)`. Guardar las últimas ~30 líneas en el report.

- [ ] **Step 6: Commit**

```bash
git add tracklist.py
git commit -m "Prompt for the video when running interactively"
```
