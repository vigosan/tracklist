# Reconocimiento en paralelo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconocer las muestras de cada hueco en paralelo con concurrencia que se ajusta sola a los rate limits de Shazam, reduciendo el tiempo total sin cambiar resultados ni eventos.

**Architecture:** Una clase pura `Limiter` en `tracklist.py` decide cuántas muestras van a la vez (arranca en 3, baja a la mitad ante cada rate limit, sube tras 6 aciertos). El bucle de muestras pasa de secuencial a `asyncio.gather` con semáforo; los eventos `sample` se emiten según llegan y la lista acumulada se mantiene ordenada por offset con `bisect.insort`, que es lo que `merge_samples` necesita.

**Tech Stack:** Python stdlib (`asyncio`, `bisect`), unittest.

## Global Constraints

- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `python3 -m unittest test_tracklist -v` con el python del sistema (24 pasan ahora).
- El contrato de eventos no cambia: `("gap", i, total, start, end)`, `("sample", offset, match|None)`, `("backoff",)`, `("done", n, segundos)`.
- `merge_samples` debe seguir recibiendo muestras ordenadas por offset ascendente (de eso depende `gap_residuals` del TUI).
- El comportamiento de `recognize_gaps` (camino pentest) y el del TUI no cambian salvo en velocidad.
- La pausa (`resume`) sigue funcionando: se espera antes de cada descarga y antes de cada muestra.
- Spec de referencia: `docs/superpowers/specs/2026-08-12-parallel-shazam-design.md`.

---

### Task 1: `Limiter` — concurrencia adaptativa

**Files:**
- Modify: `tracklist.py` (clase nueva tras `plan_samples`; constantes junto a `SAMPLE_LEAD`)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: nada.
- Produces: `START_WORKERS = 3`, `MAX_WORKERS = 3`, `RECOVER_AFTER = 6`, y la clase `Limiter` con: atributo `workers` (int), `throttle()` (baja la concurrencia y resetea el contador), `reward()` (cuenta un acierto y sube la concurrencia al llegar a `RECOVER_AFTER`). Task 2 la consume.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class LimiterTest(unittest.TestCase):
    def test_starts_at_three_and_halves_on_throttle(self):
        limiter = tracklist.Limiter()
        self.assertEqual(limiter.workers, 3)
        limiter.throttle()
        self.assertEqual(limiter.workers, 1)
        limiter.throttle()
        self.assertEqual(limiter.workers, 1)

    def test_recovers_one_worker_after_enough_clean_samples(self):
        limiter = tracklist.Limiter()
        limiter.throttle()
        for _ in range(tracklist.RECOVER_AFTER - 1):
            limiter.reward()
        self.assertEqual(limiter.workers, 1)
        limiter.reward()
        self.assertEqual(limiter.workers, 2)

    def test_never_grows_beyond_the_maximum(self):
        limiter = tracklist.Limiter()
        for _ in range(tracklist.RECOVER_AFTER * 3):
            limiter.reward()
        self.assertEqual(limiter.workers, 3)

    def test_throttle_resets_the_recovery_counter(self):
        limiter = tracklist.Limiter()
        limiter.throttle()
        for _ in range(tracklist.RECOVER_AFTER - 1):
            limiter.reward()
        limiter.throttle()
        for _ in range(tracklist.RECOVER_AFTER - 1):
            limiter.reward()
        self.assertEqual(limiter.workers, 1)
```

El caso 3→1 codifica la regla `max(1, workers // 2)`: con 3 workers, la mitad entera es 1. El último test protege que un rate limit borra el progreso hacia la recuperación.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'Limiter'`

- [ ] **Step 3: Write minimal implementation**

En `tracklist.py`, junto a `SAMPLE_LEAD`:

```python
START_WORKERS = 3
MAX_WORKERS = 3
RECOVER_AFTER = 6
```

Y tras `plan_samples`:

```python
class Limiter:
    def __init__(self):
        self.workers = START_WORKERS
        self.clean = 0

    def throttle(self):
        self.workers = max(1, self.workers // 2)
        self.clean = 0

    def reward(self):
        self.clean += 1
        if self.clean >= RECOVER_AFTER:
            self.workers = min(MAX_WORKERS, self.workers + 1)
            self.clean = 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (28 tests)

- [ ] **Step 5: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Add an adaptive limiter for Shazam concurrency"
```

---

### Task 2: bucle de muestras en paralelo

**Files:**
- Modify: `tracklist.py` (`recognize_gaps_async`: import de `bisect` y el bucle interno de muestras)

**Interfaces:**
- Consumes: `Limiter` (Task 1), `plan_samples`, `cut_sample`, `merge_samples`.
- Produces: mismo contrato de `recognize_gaps_async`; solo cambia la implementación interna.

- [ ] **Step 1: Write implementation**

Es código de red/concurrencia; se verifica con los pasos siguientes, no con unit test propio.

a) Añadir `import bisect` a los imports de `tracklist.py` (alfabético: entre `asyncio` y `hashlib`).

b) Dentro de `run()`, sustituir el bucle de muestras actual, que va desde `samples = []` hasta `suggestions += merge_samples(samples)` (ambas líneas incluidas), por:

```python
                samples = []
                limiter = Limiter()
                semaphore = asyncio.Semaphore(limiter.workers)
                warned = False

                async def handle(offset):
                    nonlocal warned
                    if resume is not None:
                        await resume.wait()
                    async with semaphore:
                        sample = await asyncio.to_thread(cut_sample, source, offset - start, directory)
                        try:
                            result = await shazam.recognize_song(str(sample))
                        except FailedDecodeJson:
                            if not warned:
                                warned = True
                                progress(("backoff",))
                            limiter.throttle()
                            await asyncio.sleep(30)
                            try:
                                result = await shazam.recognize_song(str(sample))
                            except FailedDecodeJson:
                                result = {}
                        except Exception:
                            result = {}
                        else:
                            limiter.reward()
                    track = result.get("track")
                    match = None
                    if track:
                        match = {"artist": track.get("subtitle", "?"), "title": track.get("title", "?")}
                    bisect.insort(samples, (offset, match), key=lambda item: item[0])
                    progress(("sample", offset, match))

                await asyncio.gather(*[handle(offset) for offset in plan_samples(start, end)])
                suggestions += merge_samples(samples)
```

Notas de comportamiento que este código preserva a propósito: la pausa se respeta antes de ocupar un hueco del semáforo; el evento `("backoff",)` se emite una sola vez por hueco (como hoy, que solo lo emitía el primer fallo que ocurría); y `merge_samples` recibe `samples` ya ordenado por offset gracias a `bisect.insort`.

- [ ] **Step 2: Run unit tests**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (28 tests)

- [ ] **Step 3: Verify order is preserved with a fake recognizer**

Este test comprueba lo que más riesgo tiene: que las muestras acaben ordenadas aunque los resultados lleguen en desorden. Ejecutar:

```bash
.venv/bin/python3 -c "
import asyncio, random, tracklist

class FakeShazam:
    async def recognize_song(self, path):
        await asyncio.sleep(random.uniform(0.01, 0.2))
        return {'track': {'subtitle': 'A', 'title': 'T'}}

events = []
tracklist.download_gap = lambda *a: __import__('pathlib').Path('/tmp/fake.mp3')
tracklist.cut_sample = lambda *a: __import__('pathlib').Path('/tmp/fake.wav')
import shazamio
shazamio.Shazam = FakeShazam
res = tracklist.recognize_gaps('vid', [(0, 700)], events.append)
offsets = [e[1] for e in events if e[0] == 'sample']
print('EN ORDEN:', offsets == sorted(offsets) or 'llegan desordenados (esperado)')
print('SUGERENCIAS:', len(res), 'start:', res[0]['start'] if res else None)
"
```

Expected: imprime `SUGERENCIAS: 1` y `start: 15` — una única sugerencia fusionada que empieza en el primer offset, lo que demuestra que `merge_samples` recibió las muestras ordenadas pese a que los resultados llegaron en desorden. Si `start` no es 15, el orden se rompió: PARAR y reportar.

- [ ] **Step 4: Verify the pentest path end to end**

Run: `.venv/bin/python3 tracklist.py --from-json sample.json --duration 600 _6r0Kd41jfs 2>&1 >/dev/null | head -6`
Expected: líneas `[*] hueco 1/...` y varias `[+]`/`[-]`. Interrumpir tras ~120 s y anotar lo observado. Confirma que descarga, corte y reconocimiento siguen funcionando en el camino real.

- [ ] **Step 5: Commit**

```bash
git add tracklist.py
git commit -m "Recognize gap samples concurrently"
```

---

### Task 3: medición en vivo

**Files:** ninguno (solo verificación y report).

**Interfaces:** ninguna.

- [ ] **Step 1: Time the full run**

Run:

```bash
time .venv/bin/python3 tracklist.py _6r0Kd41jfs > /tmp/parallel_run.txt 2> /tmp/parallel_run.err
```

Timeout 900000 ms. Ejecutarlo UNA sola vez y no hacer polling.

- [ ] **Step 2: Report the numbers**

Extraer y anotar en el report:

```bash
tail -1 /tmp/parallel_run.txt
grep -c "rate limit" /tmp/parallel_run.err
grep -c "^\[+\]" /tmp/parallel_run.err
grep -c "^\[-\]" /tmp/parallel_run.err
```

Comparar con la referencia secuencial conocida: ~10 minutos, resumen
`15 identificados, 10 sugeridos, 9 huecos (26:46 sin identificar de 1:03:05)`.

Criterio de éxito: el tiempo total baja de forma clara (objetivo: menos de
7 minutos) y el número de sugerencias es comparable (±3) al secuencial. Si
el tiempo NO baja o las sugerencias caen mucho, reportarlo como
DONE_WITH_CONCERNS con los números exactos, sin adornar.

- [ ] **Step 3: No commit**

Esta tarea no toca código. Si los números son malos, el report es el
entregable y la decisión de ajustar `START_WORKERS` la toma el controlador.
