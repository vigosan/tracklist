# Sesión automática desde Brave — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tracklist <video>` funciona siempre sin DevTools: lee cookies frescas de Brave vía yt-dlp y recalcula `SAPISIDHASH` en cada ejecución.

**Architecture:** `tracklist.py` sigue siendo un único script. `session.json` (capturado una vez con `--save-session`) aporta cabeceras y `context`; en cada ejecución se sobrescriben `cookie` y `authorization` con cookies volcadas de Brave por yt-dlp y un hash recién calculado. Tres funciones puras nuevas (testables sin red) más una función con subprocess.

**Tech Stack:** Python 3.14 stdlib (`hashlib`, `subprocess`, `tempfile`, `time`, `http.cookiejar`, `unittest`). Runtime: `yt-dlp` CLI (ya instalado vía Homebrew).

## Global Constraints

- Sin dependencias Python nuevas: solo stdlib; yt-dlp se usa como CLI externo.
- Sin flags nuevos en el CLI; `--save-session` y `--from-json` no cambian de comportamiento.
- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Spec de referencia: `docs/superpowers/specs/2026-08-10-auto-session-design.md`.
- Los tests se ejecutan con `python3 -m unittest test_tracklist -v` desde la raíz del repo.
- Origen para el hash: exactamente `https://studio.youtube.com`.

---

### Task 1: `build_authorization` — hash SAPISID con timestamp actual

**Files:**
- Modify: `tracklist.py` (añadir función tras `load_session`, y `import hashlib` junto al resto de imports)
- Test: `test_tracklist.py` (crear)

**Interfaces:**
- Consumes: nada de tareas anteriores.
- Produces: `build_authorization(cookies: dict[str, str], timestamp: int) -> str` — devuelve el valor completo de la cabecera `authorization`. Las claves usadas del dict son `SAPISID`, `__Secure-1PAPISID`, `__Secure-3PAPISID`. Task 3 y Task 4 la consumen.

- [ ] **Step 1: Write the failing test**

Crear `test_tracklist.py`:

```python
import hashlib
import unittest

import tracklist


def sha(value, timestamp):
    return hashlib.sha1(
        f"{timestamp} {value} https://studio.youtube.com".encode()
    ).hexdigest()


class BuildAuthorizationTest(unittest.TestCase):
    def test_binds_each_cookie_variant_to_timestamp_and_origin(self):
        cookies = {
            "SAPISID": "abc",
            "__Secure-1PAPISID": "def",
            "__Secure-3PAPISID": "ghi",
        }
        auth = tracklist.build_authorization(cookies, 1000)
        self.assertEqual(
            auth,
            f"SAPISIDHASH 1000_{sha('abc', 1000)} "
            f"SAPISID1PHASH 1000_{sha('def', 1000)} "
            f"SAPISID3PHASH 1000_{sha('ghi', 1000)}",
        )


if __name__ == "__main__":
    unittest.main()
```

Los tres valores de cookie son distintos a propósito: si la implementación cruza qué cookie alimenta qué variante, el test falla.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'build_authorization'`

- [ ] **Step 3: Write minimal implementation**

En `tracklist.py`, añadir `import hashlib` a los imports (orden alfabético: entre `argparse` y `json`) y tras `load_session`:

```python
ORIGIN = "https://studio.youtube.com"


def build_authorization(cookies, timestamp):
    def digest(value):
        return hashlib.sha1(f"{timestamp} {value} {ORIGIN}".encode()).hexdigest()

    return " ".join(
        f"{scheme} {timestamp}_{digest(cookies[name])}"
        for scheme, name in (
            ("SAPISIDHASH", "SAPISID"),
            ("SAPISID1PHASH", "__Secure-1PAPISID"),
            ("SAPISID3PHASH", "__Secure-3PAPISID"),
        )
    )
```

(La constante `ORIGIN` va junto a `BASE` al inicio del fichero.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (1 test)

- [ ] **Step 5: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Calculate SAPISIDHASH authorization from cookies"
```

---

### Task 2: `parse_cookie_file` — cookies de youtube.com desde fichero Netscape

**Files:**
- Modify: `tracklist.py` (función junto a `build_authorization`; añadir `from http.cookiejar import MozillaCookieJar` a los imports)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: nada.
- Produces: `parse_cookie_file(path: Path | str) -> dict[str, str]` — nombre → valor, solo cookies cuyo dominio termina en `youtube.com`. Task 4 la consume.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py` (con `import tempfile` y `from pathlib import Path` arriba):

```python
NETSCAPE = """\
# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tabc
studio.youtube.com\tFALSE\t/\tTRUE\t0\tSESSION\txyz
.google.com\tTRUE\t/\tTRUE\t0\tNID\tnope
"""


class ParseCookieFileTest(unittest.TestCase):
    def test_keeps_youtube_cookies_and_drops_other_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(NETSCAPE)
            cookies = tracklist.parse_cookie_file(path)
        self.assertEqual(cookies, {"SAPISID": "abc", "SESSION": "xyz"})
```

La cookie con expiración `0` (de sesión) debe sobrevivir: el test protege los flags `ignore_discard`/`ignore_expires`, que es donde este parseo suele romperse.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'parse_cookie_file'`

- [ ] **Step 3: Write minimal implementation**

```python
def parse_cookie_file(path):
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar if c.domain.endswith("youtube.com")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Parse youtube.com cookies from Netscape cookie file"
```

---

### Task 3: `refresh_headers` — pisar cookie y authorization sobre la sesión guardada

**Files:**
- Modify: `tracklist.py` (función junto a las anteriores)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `build_authorization` (Task 1).
- Produces: `refresh_headers(headers: dict, cookies: dict[str, str], timestamp: int) -> dict` — copia de `headers` con `cookie` y `authorization` regenerados y el resto intacto. Task 4 la consume.

- [ ] **Step 1: Write the failing test**

Añadir a `test_tracklist.py`:

```python
class RefreshHeadersTest(unittest.TestCase):
    def test_replaces_auth_and_cookie_but_preserves_the_rest(self):
        stored = {
            "cookie": "SAPISID=old",
            "authorization": "SAPISIDHASH 1_dead",
            "user-agent": "Mozilla/5.0",
        }
        cookies = {
            "SAPISID": "abc",
            "__Secure-1PAPISID": "def",
            "__Secure-3PAPISID": "ghi",
        }
        headers = tracklist.refresh_headers(stored, cookies, 1000)
        self.assertEqual(headers["cookie"], "SAPISID=abc; __Secure-1PAPISID=def; __Secure-3PAPISID=ghi")
        self.assertEqual(headers["authorization"], tracklist.build_authorization(cookies, 1000))
        self.assertEqual(headers["user-agent"], "Mozilla/5.0")
        self.assertEqual(stored["cookie"], "SAPISID=old")
```

La última aserción protege que el dict original no se mute: `session.json` en memoria no debe quedar contaminado.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_tracklist -v`
Expected: FAIL con `AttributeError: module 'tracklist' has no attribute 'refresh_headers'`

- [ ] **Step 3: Write minimal implementation**

```python
def refresh_headers(headers, cookies, timestamp):
    return {
        **headers,
        "cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "authorization": build_authorization(cookies, timestamp),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Rebuild cookie and authorization headers from fresh cookies"
```

---

### Task 4: `fresh_session` — volcado de Brave con yt-dlp y cableado en `main`

**Files:**
- Modify: `tracklist.py` — nuevas funciones `brave_cookies` y `fresh_session`; en `main()` sustituir `session = load_session()` por `session = fresh_session()`; actualizar el mensaje del 401 en `post()`; añadir `import subprocess`, `import tempfile`, `import time` a los imports.

**Interfaces:**
- Consumes: `parse_cookie_file` (Task 2), `refresh_headers` (Task 3), `load_session` (existente).
- Produces: `fresh_session() -> dict` con la misma forma que `load_session()` (`{"headers": ..., "context": ...}`); es lo que `main()` pasa a `fetch_claims`/`fetch_videos`.

- [ ] **Step 1: Write implementation**

Esta tarea es integración con subprocess y red; no lleva unit test (los mensajes de salida se verifican a mano en los pasos siguientes). Añadir a `tracklist.py`:

```python
def brave_cookies():
    with tempfile.TemporaryDirectory() as tmp:
        jar_path = Path(tmp) / "cookies.txt"
        command = [
            "yt-dlp", "--quiet", "--no-warnings", "--ignore-errors", "--skip-download",
            "--cookies-from-browser", "brave", "--cookies", str(jar_path),
            "https://www.youtube.com/robots.txt",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit("yt-dlp no esta instalado (brew install yt-dlp), o usa --save-session.")
        if not jar_path.exists():
            sys.exit(
                "No se pudieron leer las cookies de Brave:\n"
                f"{result.stderr.strip()[-300:]}\n"
                "Alternativa manual: captura un cURL y usa --save-session."
            )
        return parse_cookie_file(jar_path)


def fresh_session():
    session = load_session()
    cookies = brave_cookies()
    if "SAPISID" not in cookies:
        sys.exit("Brave no tiene sesion de YouTube iniciada: entra en studio.youtube.com y reintenta.")
    session["headers"] = refresh_headers(session["headers"], cookies, int(time.time()))
    return session
```

En `main()`:

```python
        session = fresh_session()
```

(era `session = load_session()`).

En `post()`, el mensaje del 401/403 pasa a:

```python
            sys.exit(
                "YouTube rechazo la sesion: entra en studio.youtube.com con Brave"
                " y reintenta, o captura un cURL nuevo y usa --save-session."
            )
```

- [ ] **Step 2: Run unit tests to verify nothing broke**

Run: `python3 -m unittest test_tracklist -v`
Expected: PASS (3 tests)

- [ ] **Step 3: Verify the yt-dlp dump in isolation**

Run: `python3 -c "import tracklist; c = tracklist.brave_cookies(); print(sorted(k for k in c if 'APISID' in k))"`
Expected: lista que incluye `SAPISID`, `__Secure-1PAPISID`, `__Secure-3PAPISID`. La primera vez macOS pedirá acceso al Llavero ("Brave Safe Storage"): aceptar. Si la lista sale vacía o falla, parar y revisar antes de seguir (probable causa: Brave sin sesión de YouTube o acceso al Llavero denegado).

- [ ] **Step 4: Live end-to-end check**

Run: `python3 tracklist.py _6r0Kd41jfs`
Expected: tracklist impreso con timestamps (o `Sin reclamaciones con timestamp para este video.` si el vídeo no tiene claims con timestamp). NO debe aparecer "Sesion caducada" ni "YouTube rechazo la sesion".

- [ ] **Step 5: Commit**

```bash
git add tracklist.py
git commit -m "Read fresh Brave cookies via yt-dlp on every run"
```
