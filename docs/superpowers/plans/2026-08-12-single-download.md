# Descarga única del audio — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Descargar el audio del vídeo una sola vez (28 s medidos) en lugar de un tramo por hueco con `--download-sections` (8 min por tramo medidos), y cortar todas las muestras de ese fichero local.

**Architecture:** `download_gap` se sustituye por `download_audio(video_id, directory)`. `recognize_gaps_async` descarga una vez antes del bucle de huecos y pasa offsets absolutos a `cut_sample`. Todo lo demás (eventos, Limiter, Gate, orden por `bisect.insort`, pausa) queda igual.

**Tech Stack:** Python stdlib, yt-dlp y ffmpeg como CLIs externos, unittest.

## Global Constraints

- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` con el python del sistema (30 pasan ahora).
- El contrato de eventos no cambia: `("gap", i, total, start, end)`, `("sample", offset, match|None)`, `("backoff",)`, `("done", n, segundos)`.
- La pausa (`resume`) se sigue respetando antes de la descarga y antes de cada muestra.
- El camino sin reconocimiento y el TUI no cambian salvo en velocidad.
- Spec de referencia: `docs/superpowers/specs/2026-08-12-single-download-design.md`.

---

### Task 1: `download_audio` y offsets absolutos

**Files:**
- Modify: `tracklist.py` (sustituir `download_gap`; ajustar el bucle de `recognize_gaps_async`)

**Interfaces:**
- Consumes: nada nuevo.
- Produces: `download_audio(video_id, directory) -> Path | None` (reemplaza a `download_gap`). `cut_sample` no cambia de firma, pero pasa a recibir offsets absolutos.

- [ ] **Step 1: Replace download_gap with download_audio**

En `tracklist.py`, sustituir la función completa (líneas 291-304, desde `def download_gap(video_id, start, end, directory):` hasta su `return matches[0]`) por:

```python
def download_audio(video_id, directory):
    command = [
        "yt-dlp", "--quiet", "--no-warnings", "-x", "--audio-format", "mp3",
        "--cookies-from-browser", "brave",
        "-o", str(directory / "audio.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    matches = list(directory.glob("audio.*"))
    if result.returncode != 0 or not matches:
        print("Aviso: no se pudo descargar el audio del video", file=sys.stderr)
        return None
    return matches[0]
```

- [ ] **Step 2: Download once, before the gap loop**

En `recognize_gaps_async`, dentro de `run()`, el bloque actual es:

```python
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index, (start, end) in enumerate(gaps, 1):
                progress(("gap", index, len(gaps), start, end))
                if resume is not None:
                    await resume.wait()
                source = await asyncio.to_thread(download_gap, video_id, start, end, directory)
                if source is None:
                    continue
```

Sustituirlo por:

```python
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            if resume is not None:
                await resume.wait()
            source = await asyncio.to_thread(download_audio, video_id, directory)
            if source is None:
                progress(("done", 0, int(time.monotonic() - began)))
                return []
            for index, (start, end) in enumerate(gaps, 1):
                progress(("gap", index, len(gaps), start, end))
```

- [ ] **Step 3: Cut with absolute offsets**

En el mismo `run()`, dentro de `handle`, la línea del corte es:

```python
                        sample = await asyncio.to_thread(cut_sample, source, offset - start, directory)
```

Sustituirla por:

```python
                        sample = await asyncio.to_thread(cut_sample, source, offset, directory)
```

- [ ] **Step 4: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (30 tests)

- [ ] **Step 5: Verify with doubles that there is one download and absolute offsets**

Esta comprobación no toca la red: sustituye `download_audio`, `cut_sample` y Shazam por dobles y observa las llamadas.

```bash
.venv/bin/python3 -c "
import asyncio, pathlib, tracklist

downloads = []
cuts = []

def fake_download(video_id, directory):
    downloads.append(video_id)
    return pathlib.Path('/tmp/fake-audio.mp3')

def fake_cut(source, offset, directory):
    cuts.append(offset)
    return pathlib.Path(f'/tmp/fake-{offset}.wav')

class FakeShazam:
    async def recognize_song(self, path):
        return {}

tracklist.download_audio = fake_download
tracklist.cut_sample = fake_cut
import shazamio
shazamio.Shazam = FakeShazam

events = []
tracklist.recognize_gaps('vid12345678', [(0, 300), (600, 900)], events.append)
print('DESCARGAS:', len(downloads))
print('OFFSETS:', cuts)
"
```

Expected exacto: `DESCARGAS: 1` y `OFFSETS: [15, 75, 135, 195, 255, 615, 675, 735, 795, 855]` (en cualquier orden, ya que las muestras van en paralelo — ordenar mentalmente antes de comparar). Lo esencial: **una sola descarga** y que los offsets del segundo hueco sean **615+** (absolutos), no 15+ (relativos). Si aparecen offsets repetidos entre huecos, el cambio de la Task 1 Step 3 no se aplicó: PARAR y reportar.

- [ ] **Step 6: Verify the pentest path really recognizes**

Run: `.venv/bin/python3 tracklist.py --from-json sample.json --duration 600 _6r0Kd41jfs 2>&1 >/dev/null | head -5`
Expected: tras un arranque de ~30 s (la descarga completa), líneas `[*] hueco 1/...` y varias `[+]`/`[-]`. Interrumpir a los ~180 s y anotar lo observado, incluido cuánto tardó en aparecer la primera línea de muestra.

- [ ] **Step 7: Commit**

```bash
git add tracklist.py
git commit -m "Download the whole audio once instead of per gap"
```

---

### Task 2: medición en vivo

**Files:** ninguno (verificación y report).

**Interfaces:** ninguna.

- [ ] **Step 1: Time the full run**

Run en segundo plano (la herramienta síncrona corta a los 10 min):

```bash
START=$(date +%s); .venv/bin/python3 tracklist.py _6r0Kd41jfs > /tmp/single_run.txt 2> /tmp/single_run.err; echo "EXIT=$? SEGUNDOS=$(( $(date +%s) - START ))" > /tmp/single_run.time
```

Lanzarlo UNA vez con `run_in_background: true` y esperar la notificación. No hacer polling con sleeps.

- [ ] **Step 2: Report the numbers**

```bash
cat /tmp/single_run.time
tail -1 /tmp/single_run.txt
grep -c "rate limit" /tmp/single_run.err
grep -c "^\[+\]" /tmp/single_run.err
```

Referencias con las que comparar:
- Secuencial con descarga por tramo: ~10 min de reconocimiento, pero la ejecución de la rama anterior llevaba 55 min sin terminar por las descargas.
- Resumen de referencia: `15 identificados, 10 sugeridos, 9 huecos (26:46 sin identificar de 1:03:05)`.

Criterio de éxito: ejecución completa en **menos de 10 minutos** con un número de sugerencias comparable (±4). Si no se cumple, reportar DONE_WITH_CONCERNS con los números exactos y sin adornar.

- [ ] **Step 3: No commit**

Esta tarea no toca código.
