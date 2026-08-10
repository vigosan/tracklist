# Reconocimiento de huecos con Shazam — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tracklist.py --recognize <video>` rellena los huecos `?????` con sugerencias de Shazam marcadas con ` (?)`.

**Architecture:** `tracklist.py` sigue siendo un único script. Tres funciones puras nuevas (`plan_samples`, `merge_samples`, cambios en `render`) se testean con unittest; la orquestación de red (`download_gap`, `cut_sample`, `recognize_gaps`) usa yt-dlp + ffmpeg + shazamio y se verifica en vivo. shazamio se importa perezosamente solo en el camino `--recognize`.

**Tech Stack:** Python stdlib (`subprocess`, `tempfile`, `asyncio`, `unittest`) + CLIs externos yt-dlp y ffmpeg + `shazamio` (pip, solo en `.venv`).

## Global Constraints

- El flujo sin `--recognize` sigue siendo stdlib puro y debe correr con el python del sistema (shazamio solo se importa dentro del camino `--recognize`).
- `shazamio` se instala únicamente en el `.venv` del proyecto: `.venv/bin/pip install shazamio`.
- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` desde la raíz del repo.
- Muestreo exacto del spec: muestras de 10 s, la primera en `inicio_hueco + 15 s`, luego cada 60 s, solo si la muestra completa cabe (`offset + 10 <= fin_hueco`).
- Marcador de sugerencia: ` (?)` tras `Artista - Tema`.
- Resumen: `{n} identificados, {s} sugeridos, {k} huecos ({t} sin identificar de {total})`, y la parte `{s} sugeridos, ` solo aparece cuando hay sugerencias.
- Spec de referencia: `docs/superpowers/specs/2026-08-10-recognize-gaps-design.md`.

---

### Task 1: `plan_samples` — offsets de muestreo dentro de un hueco

**Files:**
- Modify: `tracklist.py` (constantes junto a `MIN_GAP_SECONDS`; función tras `find_gaps`)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: nada.
- Produces: `plan_samples(start: int, end: int) -> list[int]` (offsets absolutos en segundos) y las constantes `SAMPLE_SECONDS = 10`, `SAMPLE_INTERVAL = 60`, `SAMPLE_LEAD = 15`. Tasks 2 y 4 las consumen.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class PlanSamplesTest(unittest.TestCase):
    def test_samples_every_interval_while_they_fit(self):
        self.assertEqual(tracklist.plan_samples(0, 90), [15, 75])
        self.assertEqual(tracklist.plan_samples(120, 210), [135, 195])

    def test_sample_that_does_not_fit_is_dropped(self):
        self.assertEqual(tracklist.plan_samples(0, 84), [15])
        self.assertEqual(tracklist.plan_samples(0, 24), [])
```

El caso `(0, 84)` protege el borde: la muestra en 75 acabaría en 85 > 84 y debe descartarse; el hueco mínimo real (90 s) produce exactamente 2 muestras.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'plan_samples'`

- [ ] **Step 3: Write minimal implementation**

En `tracklist.py`, junto a `MIN_GAP_SECONDS`:

```python
SAMPLE_SECONDS = 10
SAMPLE_INTERVAL = 60
SAMPLE_LEAD = 15
```

Tras `find_gaps`:

```python
def plan_samples(start, end):
    return list(range(start + SAMPLE_LEAD, end - SAMPLE_SECONDS + 1, SAMPLE_INTERVAL))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (5 tests: 3 existentes + 2 nuevos)

- [ ] **Step 5: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Plan recognition sample offsets within gaps"
```

---

### Task 2: `merge_samples` — fusionar rachas de muestras en sugerencias

**Files:**
- Modify: `tracklist.py` (función tras `plan_samples`)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `SAMPLE_SECONDS` (Task 1).
- Produces: `merge_samples(samples: list[tuple[int, dict | None]]) -> list[dict]`. Cada tupla es `(offset_absoluto, match)` donde `match` es `{"artist": str, "title": str}` o `None`. Cada sugerencia devuelta tiene la misma forma que las entradas de Content ID: `{"artist", "title", "label": "", "start", "duration", "suggested": True}`. Tasks 3 y 4 la consumen.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class MergeSamplesTest(unittest.TestCase):
    def test_consecutive_matches_collapse_into_one_suggestion(self):
        first = {"artist": "Kate Project", "title": "A Better World"}
        second = {"artist": "Wand", "title": "Happiness"}
        samples = [(15, first), (75, first), (135, second)]
        self.assertEqual(
            tracklist.merge_samples(samples),
            [
                {"artist": "Kate Project", "title": "A Better World", "label": "", "start": 15, "duration": 70, "suggested": True},
                {"artist": "Wand", "title": "Happiness", "label": "", "start": 135, "duration": 10, "suggested": True},
            ],
        )

    def test_missing_result_breaks_a_run(self):
        match = {"artist": "X", "title": "Y"}
        samples = [(15, match), (75, None), (135, match)]
        merged = tracklist.merge_samples(samples)
        self.assertEqual(
            [(s["start"], s["duration"]) for s in merged],
            [(15, 10), (135, 10)],
        )
```

La duración 70 codifica la regla del spec: `última_muestra + 10 − primera_muestra` (75 + 10 − 15). El segundo test protege que un silencio o muestra sin resultado parte la racha en dos sugerencias.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'merge_samples'`

- [ ] **Step 3: Write minimal implementation**

```python
def merge_samples(samples):
    suggestions = []
    previous = None
    for offset, match in samples:
        if match is None:
            previous = None
            continue
        if previous and (previous["artist"], previous["title"]) == (match["artist"], match["title"]):
            previous["duration"] = offset + SAMPLE_SECONDS - previous["start"]
            continue
        previous = {**match, "label": "", "start": offset, "duration": SAMPLE_SECONDS, "suggested": True}
        suggestions.append(previous)
    return suggestions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Merge consecutive matching samples into suggestions"
```

---

### Task 3: `render` — marcador ` (?)` y resumen con sugeridos

**Files:**
- Modify: `tracklist.py` (función `render` existente, dos cambios puntuales)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: entradas con la clave opcional `suggested` (forma de Task 2).
- Produces: `render` acepta entradas mezcladas (Content ID + sugeridas) sin cambio de firma. Task 4 depende de este comportamiento.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class RenderSuggestionsTest(unittest.TestCase):
    def test_marks_suggested_entries_and_counts_them_separately(self):
        entries = [
            {"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60},
            {"artist": "B", "title": "U", "label": "", "start": 60, "duration": 30, "suggested": True},
        ]
        text = tracklist.render(entries, [], "video123456", False)
        self.assertIn("  - 00:00 A - T\n", text)
        self.assertIn("  - 01:00 B - U (?)", text)
        self.assertIn("1 identificados, 1 sugeridos, 0 huecos", text)

    def test_summary_omits_suggestions_when_none(self):
        entries = [{"artist": "A", "title": "T", "label": "", "start": 0, "duration": 60}]
        text = tracklist.render(entries, [], "video123456", False)
        self.assertIn("1 identificados, 0 huecos", text)
        self.assertNotIn("sugeridos", text)
```

El `\n` en la primera aserción garantiza que la entrada NO sugerida no lleva marcador. El segundo test protege que el tracklist actual no cambia ni un carácter cuando no hay sugerencias.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL — `AssertionError` en las aserciones de `(?)` y `sugeridos` (render aún no los emite)

- [ ] **Step 3: Write minimal implementation**

En `render`, sustituir:

```python
        line = f"  - {hhmmss(start)} {item['artist']} - {item['title']}"
        if show_label and item["label"]:
```

por:

```python
        line = f"  - {hhmmss(start)} {item['artist']} - {item['title']}"
        if item.get("suggested"):
            line += " (?)"
        if show_label and item["label"]:
```

Y sustituir el bloque final:

```python
    identified = sum(entry["duration"] for entry in entries)
    missing = sum(end - start for start, end in gaps)
    lines += [
        "",
        f"{len(entries)} identificados, {len(gaps)} huecos "
        f"({hhmmss(missing)} sin identificar de {hhmmss(identified + missing)})",
    ]
```

por:

```python
    identified = sum(entry["duration"] for entry in entries)
    missing = sum(end - start for start, end in gaps)
    suggested = sum(1 for entry in entries if entry.get("suggested"))
    suggested_part = f"{suggested} sugeridos, " if suggested else ""
    lines += [
        "",
        f"{len(entries) - suggested} identificados, {suggested_part}{len(gaps)} huecos "
        f"({hhmmss(missing)} sin identificar de {hhmmss(identified + missing)})",
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Regression check del flujo actual**

Run: `python3 tracklist.py --from-json sample.json abc123def45 | tail -2`
Expected: la última línea dice exactamente `46 identificados, 9 huecos (31:21 sin identificar de 2:31:18)` — sin la palabra `sugeridos`.

- [ ] **Step 6: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Render suggested tracks with marker and summary count"
```

---

### Task 4: `recognize_gaps` — descarga, muestreo, Shazam y cableado en `main`

**Files:**
- Modify: `tracklist.py` — funciones `download_gap`, `cut_sample`, `recognize_gaps`; flag `--recognize` en `main()`; `import asyncio` en los imports.

**Interfaces:**
- Consumes: `plan_samples` (Task 1), `merge_samples` (Task 2), `render` con sugerencias (Task 3), `find_gaps`, `hhmmss` (existentes), constantes `SAMPLE_SECONDS`.
- Produces: `recognize_gaps(video_id: str, gaps: list[tuple[int, int]]) -> list[dict]` (lista de sugerencias con la forma de Task 2). `main()` la invoca solo con `--recognize` y huecos presentes.

- [ ] **Step 1: Install shazamio in the project venv**

Run: `.venv/bin/pip install shazamio && .venv/bin/python3 -c "import shazamio; print('shazamio OK')"`
Expected: `shazamio OK`. Si la instalación falla en Python 3.14 (falta de wheels de alguna dependencia): STOP, reportar BLOCKED con el error — la spec dicta decidir con el usuario (bajar la versión de python del venv u otra alternativa). No intentar workarounds por cuenta propia.

- [ ] **Step 2: Write implementation**

Esta tarea es integración con red y CLIs externos; no lleva unit test (verificación manual en los pasos siguientes). Añadir `import asyncio` a los imports (alfabético, antes de `hashlib`). Añadir tras `merge_samples`:

```python
def download_gap(video_id, start, end, directory):
    command = [
        "yt-dlp", "--quiet", "--no-warnings", "-x", "--audio-format", "mp3",
        "--cookies-from-browser", "brave",
        "--download-sections", f"*{start}-{end}",
        "-o", str(directory / f"gap-{start}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    matches = list(directory.glob(f"gap-{start}.*"))
    if result.returncode != 0 or not matches:
        print(f"Aviso: no se pudo descargar el hueco {hhmmss(start)}-{hhmmss(end)}", file=sys.stderr)
        return None
    return matches[0]


def cut_sample(source, offset, directory):
    target = directory / f"{source.stem}-{offset}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(offset), "-t", str(SAMPLE_SECONDS),
         "-i", str(source), str(target)],
        check=True, capture_output=True,
    )
    return target


def recognize_gaps(video_id, gaps):
    try:
        from shazamio import Shazam
    except ImportError:
        sys.exit("Falta shazamio para --recognize: .venv/bin/pip install shazamio")

    async def run():
        shazam = Shazam()
        suggestions = []
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for start, end in gaps:
                source = download_gap(video_id, start, end, directory)
                if source is None:
                    continue
                samples = []
                for offset in plan_samples(start, end):
                    sample = cut_sample(source, offset - start, directory)
                    result = await shazam.recognize(str(sample))
                    track = result.get("track")
                    match = None
                    if track:
                        match = {"artist": track.get("subtitle", "?"), "title": track.get("title", "?")}
                    samples.append((offset, match))
                    await asyncio.sleep(1)
                suggestions += merge_samples(samples)
        return suggestions

    return asyncio.run(run())
```

Nota de API: en shazamio moderno el método es `await shazam.recognize(ruta)`; si la versión instalada no lo tiene, usar `await shazam.recognize_song(ruta)` — son las dos únicas variantes; registrar en el report cuál se usó.

En `main()`, añadir el flag tras `--label`:

```python
    parser.add_argument("--recognize", action="store_true", help="identifica los huecos con Shazam")
```

Y sustituir:

```python
    gaps = find_gaps(entries, duration)
    print(render(entries, gaps, video_id, args.label, title))
```

por:

```python
    gaps = find_gaps(entries, duration)
    if args.recognize and gaps:
        entries = sorted(entries + recognize_gaps(video_id, gaps), key=lambda entry: entry["start"])
        gaps = find_gaps(entries, duration)
    print(render(entries, gaps, video_id, args.label, title))
```

- [ ] **Step 3: Run unit tests to verify nothing broke**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (9 tests)

- [ ] **Step 4: Verify the base flow still runs without shazamio**

Run: `python3 tracklist.py --from-json sample.json abc123def45 | head -3`
Expected: tracklist impreso con el python del sistema (sin shazamio instalado ahí) — el import perezoso no debe ejecutarse.

- [ ] **Step 5: Live end-to-end check**

Run: `.venv/bin/python3 tracklist.py --recognize _6r0Kd41jfs`
Expected: tarda varios minutos (descargas + ~1 s por muestra). El tracklist debe incluir al menos una línea nueva con ` (?)` dentro de los antiguos huecos, el resumen debe decir `N identificados, S sugeridos, K huecos (...)` con K < 7, y los avisos de descarga (si los hay) van por stderr sin abortar el resto. Guardar la salida completa en el report.

- [ ] **Step 6: Commit**

```bash
git add tracklist.py
git commit -m "Recognize unidentified gaps with Shazam"
```
