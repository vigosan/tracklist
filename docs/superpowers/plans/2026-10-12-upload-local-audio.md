# Subir un audio local a YouTube — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tracklist fichero.m4a` convierte el audio en vídeo, lo sube privado a YouTube, espera a que Content ID lo reclame y saca el tracklist con el flujo actual.

**Architecture:** Módulo nuevo `upload.py` (como `recognize_ui.py`: importa `tracklist`, funciones pequeñas) con la conversión ffmpeg, el registro `uploads.json`, la espera y la API de YouTube. Las librerías de Google se importan solo dentro de `youtube_client`/`upload_video`, para que los tests y el flujo por ID no las necesiten. `tracklist.main` detecta un fichero local, llama a `upload.prepare` para obtener el `video_id`, deja el mp3 en `audio_path(video_id)` y sigue por el flujo existente sin cambios.

**Tech Stack:** Python 3.14 (`.venv`), stdlib + `google-api-python-client` + `google-auth-oauthlib`, ffmpeg (`/opt/homebrew/bin/ffmpeg`), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-12-upload-local-audio-design.md`

## Antes de empezar

El árbol de trabajo tiene cambios sin commitear en `tracklist.py` y `test_tracklist.py` (merge window de sugerencias) y con ellos fallan 2 tests (`MergeSamplesTest.test_missing_result_breaks_a_run`, `MergeVariantTitlesTest.test_the_same_track_apart_in_time_stays_two_entries`). Vicent decide qué hacer con ese trabajo; NO empezar la Task 1 hasta que `.venv/bin/python -m unittest test_tracklist` pase entero.

## Global Constraints

- CERO comentarios en el código.
- Commits: título descriptivo solo, sin body, sin prefijos `feat:`/`fix:`.
- Tests: `.venv/bin/python -m unittest test_tracklist -v` desde la raíz; todos los tests nuevos van en `test_tracklist.py`.
- Mensajes al usuario en español sin tildes, como el resto del código (`"No hay sesion guardada"`).
- stdout lleva solo el tracklist; todo el progreso de subida/espera va a stderr.
- Vídeo: fondo negro 640x360 a 1 fps, audio con `-c:a copy`, `-shortest`.
- Subida: `privacyStatus: private`, título = nombre del fichero sin extensión.
- Ficheros: `~/.config/tracklist/uploads.json`, `~/.config/tracklist/client_secret.json`, `~/.config/tracklist/youtube_token.json` (chmod 600).
- Scopes: `https://www.googleapis.com/auth/youtube.upload`, `https://www.googleapis.com/auth/youtube.readonly`.
- Espera: procesado cada 30 s; reclamaciones cada 120 s; estable con 3 recuentos iguales y > 0; tope 3600 s.
- El vídeo subido NO se borra.
- `recognize_gaps`, `recognize_tui` y el flujo por URL/ID NO cambian.

## Review Focus

1. Rutas con espacios (todos los `Spook Multiespacio N.m4a` los tienen): ffmpeg recibe la ruta intacta como argumento de lista, nunca vía shell. Test en Task 1.
2. Relanzar tras subir pero antes de que acabe la espera: no hay segunda subida. Test en Task 3.
3. Subida fallida (cuota agotada): el fichero no queda registrado y el siguiente intento vuelve a subir. Test en Task 3.
4. Sin sesión de Studio (o caducada) durante la espera: no esperar 60 minutos a ciegas; avisar y seguir. Test en Task 2.
5. `--fresh` con un fichero local: el audio sale del fichero local, no de yt-dlp. Cubierto por el orden en `main` (Task 4) y por el test de que `place_audio` regenera un mp3 borrado (Task 1).

---

### Task 1: Convertir el audio local (vídeo para subir y mp3 para Shazam)

**Files:**
- Create: `upload.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `tracklist.audio_path(video_id) -> Path`, `tracklist.AUDIO_DIR`.
- Produces: `upload.convert_to_video(source: Path, target: Path) -> None`, `upload.place_audio(source: Path, video_id: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Añadir `import subprocess` y `import upload` a los imports de `test_tracklist.py` (orden alfabético, como están) y, antes del bloque `if __name__ == "__main__":`, añadir:

```python
def ffprobe_streams(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return sorted(result.stdout.split())


class LocalAudio(IsolatedCache):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "Spook Multiespacio 3.m4a"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:a", "aac", str(self.source)],
            check=True, capture_output=True,
        )


class ConvertToVideoTest(LocalAudio, unittest.TestCase):
    def test_youtube_gets_a_video_whose_audio_is_untouched(self):
        target = self.root / "out.mp4"
        upload.convert_to_video(self.source, target)
        self.assertEqual(ffprobe_streams(target), ["aac,audio", "h264,video"])


class PlaceAudioTest(LocalAudio, unittest.TestCase):
    def test_shazam_finds_the_local_audio_where_a_download_would_be(self):
        upload.place_audio(self.source, "vid12345678")
        self.assertEqual(ffprobe_streams(tracklist.audio_path("vid12345678")), ["mp3,audio"])

    def test_existing_audio_is_reused_instead_of_converted_again(self):
        tracklist.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        tracklist.audio_path("vid12345678").write_bytes(b"ya estaba")
        upload.place_audio(self.source, "vid12345678")
        self.assertEqual(tracklist.audio_path("vid12345678").read_bytes(), b"ya estaba")

    def test_audio_cleared_by_fresh_is_rebuilt_from_the_local_file(self):
        upload.place_audio(self.source, "vid12345678")
        tracklist.clear_cache("vid12345678")
        upload.place_audio(self.source, "vid12345678")
        self.assertTrue(tracklist.audio_path("vid12345678").exists())
```

Nota: `ffprobe` imprime `codec_name,codec_type`; si en esta máquina sale en otro orden (`audio,aac`), ajustar las cadenas esperadas tras verlo en el Step 2, sin cambiar lo que se comprueba (h264 + aac copiado; mp3 solo audio).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -20`
Expected: ERROR `ModuleNotFoundError: No module named 'upload'`.

- [ ] **Step 3: Write minimal implementation**

Crear `upload.py`:

```python
import subprocess

import tracklist


def convert_to_video(source, target):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:r=1",
         "-i", str(source),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-shortest", str(target)],
        check=True, capture_output=True,
    )


def place_audio(source, video_id):
    target = tracklist.audio_path(video_id)
    if target.exists():
        return
    tracklist.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn",
         "-codec:a", "libmp3lame", "-q:a", "2", str(target)],
        check=True, capture_output=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add upload.py test_tracklist.py
git commit -m "Convert a local audio file into a video and a cached mp3"
```

---

### Task 2: Esperar al procesado y a que las reclamaciones se estabilicen

**Files:**
- Modify: `upload.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `tracklist.try_claims(video_id) -> (session | None, entries: list)`, `tracklist.hhmmss(seconds: int) -> str`.
- Produces:
  - Constantes `PROCESSING_POLL = 30`, `CLAIMS_POLL = 120`, `CLAIMS_STABLE_POLLS = 3`, `CLAIMS_TIMEOUT = 3600`.
  - `upload.claims_settled(counts: list[int], elapsed: float) -> bool`.
  - `upload.processing_status(client, video_id) -> str` (sin test: envoltorio fino de la API, se prueba a mano en Task 4).
  - `upload.wait_for_processing(client, video_id, report, sleep=time.sleep) -> None`; sale con `SystemExit` si el estado es `failed`.
  - `upload.wait_for_claims(video_id, report, sleep=time.sleep, clock=time.monotonic) -> None`.
  - `report` es `callable(str)`.

- [ ] **Step 1: Write the failing tests**

Añadir antes de `if __name__ == "__main__":`:

```python
class ClaimsSettledTest(unittest.TestCase):
    def test_the_same_count_three_times_means_content_id_is_done(self):
        self.assertTrue(upload.claims_settled([2, 5, 5, 5], 480))

    def test_a_count_still_growing_keeps_waiting(self):
        self.assertFalse(upload.claims_settled([5, 5, 6], 360))

    def test_no_claims_yet_is_not_mistaken_for_done(self):
        self.assertFalse(upload.claims_settled([0, 0, 0], 360))

    def test_waiting_gives_up_after_an_hour(self):
        self.assertTrue(upload.claims_settled([0, 0, 0], upload.CLAIMS_TIMEOUT))


class FakeTime:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


class WaitForClaimsTest(unittest.TestCase):
    def wait(self, counts, session=True):
        polls = iter(counts)
        original = tracklist.try_claims
        tracklist.try_claims = lambda video_id: (
            ({} if session else None),
            [{"start": 0}] * next(polls),
        )
        time = FakeTime()
        messages = []
        try:
            upload.wait_for_claims("vid12345678", messages.append, time.sleep, time.clock)
        finally:
            tracklist.try_claims = original
        return time, messages

    def test_stops_polling_once_the_claims_stop_changing(self):
        time, _ = self.wait([0, 3, 7, 7, 7, 9])
        self.assertEqual(time.sleeps, [upload.CLAIMS_POLL] * 4)

    def test_without_a_studio_session_it_does_not_wait_blindly(self):
        time, messages = self.wait([0], session=False)
        self.assertEqual(time.sleeps, [])
        self.assertIn("sesion", messages[-1])

    def test_giving_up_tells_the_user_to_retry_later(self):
        polls = upload.CLAIMS_TIMEOUT // upload.CLAIMS_POLL + 1
        time, messages = self.wait([0] * polls)
        self.assertEqual(time.now, upload.CLAIMS_TIMEOUT)
        self.assertIn("mas tarde", messages[-1])


class WaitForProcessingTest(unittest.TestCase):
    def wait(self, statuses):
        polls = iter(statuses)
        original = upload.processing_status
        upload.processing_status = lambda client, video_id: next(polls)
        time = FakeTime()
        try:
            upload.wait_for_processing("client", "vid12345678", lambda message: None, time.sleep)
        finally:
            upload.processing_status = original
        return time

    def test_claims_are_only_read_after_youtube_finishes_processing(self):
        time = self.wait(["processing", "processing", "succeeded"])
        self.assertEqual(time.sleeps, [upload.PROCESSING_POLL] * 2)

    def test_a_failed_upload_stops_instead_of_waiting_forever(self):
        with self.assertRaises(SystemExit):
            self.wait(["processing", "failed"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -20`
Expected: ERROR `AttributeError: module 'upload' has no attribute 'claims_settled'` (y similares).

- [ ] **Step 3: Write minimal implementation**

En `upload.py`, imports al principio pasan a ser:

```python
import subprocess
import sys
import time

import tracklist

PROCESSING_POLL = 30
CLAIMS_POLL = 120
CLAIMS_STABLE_POLLS = 3
CLAIMS_TIMEOUT = 3600
```

y añadir al final:

```python
def claims_settled(counts, elapsed):
    if elapsed >= CLAIMS_TIMEOUT:
        return True
    recent = counts[-CLAIMS_STABLE_POLLS:]
    return len(recent) == CLAIMS_STABLE_POLLS and recent[0] > 0 and len(set(recent)) == 1


def processing_status(client, video_id):
    items = client.videos().list(part="processingDetails", id=video_id).execute().get("items")
    if not items:
        return "processing"
    return items[0]["processingDetails"]["processingStatus"]


def wait_for_processing(client, video_id, report, sleep=time.sleep):
    while True:
        status = processing_status(client, video_id)
        if status == "succeeded":
            return
        if status == "failed":
            sys.exit(f"YouTube no pudo procesar el video {video_id}")
        report(f"Procesando en YouTube ({status})")
        sleep(PROCESSING_POLL)


def wait_for_claims(video_id, report, sleep=time.sleep, clock=time.monotonic):
    began = clock()
    counts = []
    while True:
        session, entries = tracklist.try_claims(video_id)
        if session is None:
            report("Sin sesion de Studio: no se pueden leer las reclamaciones, sigo sin esperar")
            return
        counts.append(len(entries))
        elapsed = clock() - began
        report(f"Reclamaciones: {len(entries)} ({tracklist.hhmmss(int(elapsed))})")
        if elapsed >= CLAIMS_TIMEOUT:
            report(f"Content ID sigue trabajando: vuelve a lanzar 'tracklist {video_id}' mas tarde")
            return
        if claims_settled(counts, elapsed):
            return
        sleep(CLAIMS_POLL)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add upload.py test_tracklist.py
git commit -m "Wait for YouTube processing and for Content ID claims to settle"
```

---

### Task 3: Subir a YouTube una sola vez por fichero

**Files:**
- Modify: `upload.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `convert_to_video` (Task 1), `wait_for_processing`, `wait_for_claims` (Task 2).
- Produces:
  - Constantes `CONFIG_DIR`, `UPLOADS_FILE`, `CLIENT_SECRET`, `TOKEN_FILE`, `SCOPES`, `QUOTA_REASONS`.
  - `upload.known_upload(path: Path) -> str | None`, `upload.remember_upload(path: Path, video_id: str) -> None`.
  - `upload.youtube_client()` y `upload.upload_video(client, path: Path, title: str) -> str` (sin test: API real, se prueban a mano en Task 4).
  - `upload.prepare(path: Path, report) -> str` (devuelve el `video_id`). Task 4 lo consume.

- [ ] **Step 1: Instalar dependencias**

Run: `.venv/bin/pip install google-api-python-client google-auth-oauthlib`
Expected: instalación correcta; `.venv/bin/python -c "import googleapiclient, google_auth_oauthlib"` no imprime nada.

- [ ] **Step 2: Write the failing tests**

Añadir antes de `if __name__ == "__main__":`:

```python
class IsolatedUploads:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        original = upload.UPLOADS_FILE
        upload.UPLOADS_FILE = self.root / "config" / "uploads.json"

        def restore():
            upload.UPLOADS_FILE = original

        self.addCleanup(restore)


class UploadRegistryTest(IsolatedUploads, unittest.TestCase):
    def test_a_file_never_uploaded_is_unknown(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        self.assertIsNone(upload.known_upload(source))

    def test_a_renamed_file_is_still_recognised_by_its_content(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        upload.remember_upload(source, "vid12345678")
        renamed = self.root / "otro nombre.m4a"
        source.rename(renamed)
        self.assertEqual(upload.known_upload(renamed), "vid12345678")

    def test_a_different_recording_with_the_same_name_is_not_confused(self):
        source = self.root / "set.m4a"
        source.write_bytes(b"audio")
        upload.remember_upload(source, "vid12345678")
        source.write_bytes(b"otra grabacion")
        self.assertIsNone(upload.known_upload(source))


class PrepareTest(IsolatedUploads, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "Spook Multiespacio 3.m4a"
        self.source.write_bytes(b"audio")
        self.uploads = []
        self.waits = []
        self.upload_error = None
        names = ("youtube_client", "convert_to_video", "upload_video",
                 "wait_for_processing", "wait_for_claims")
        original = {name: getattr(upload, name) for name in names}

        def fake_upload(client, path, title):
            if self.upload_error:
                raise self.upload_error
            self.uploads.append(title)
            return "vid12345678"

        upload.youtube_client = lambda: "client"
        upload.convert_to_video = lambda source, target: target.write_bytes(b"mp4")
        upload.upload_video = fake_upload
        upload.wait_for_processing = lambda client, video_id, report: self.waits.append(("processing", video_id))
        upload.wait_for_claims = lambda video_id, report: self.waits.append(("claims", video_id))

        def restore():
            for name, value in original.items():
                setattr(upload, name, value)

        self.addCleanup(restore)

    def test_the_video_is_titled_after_the_file(self):
        self.assertEqual(upload.prepare(self.source, lambda message: None), "vid12345678")
        self.assertEqual(self.uploads, ["Spook Multiespacio 3"])

    def test_relaunching_after_an_interrupted_wait_does_not_upload_twice(self):
        upload.prepare(self.source, lambda message: None)
        upload.prepare(self.source, lambda message: None)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.waits[-2:], [("processing", "vid12345678"), ("claims", "vid12345678")])

    def test_a_failed_upload_is_retried_on_the_next_run(self):
        self.upload_error = SystemExit("cuota")
        with self.assertRaises(SystemExit):
            upload.prepare(self.source, lambda message: None)
        self.assertIsNone(upload.known_upload(self.source))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -20`
Expected: ERROR `AttributeError: module 'upload' has no attribute 'UPLOADS_FILE'`.

- [ ] **Step 4: Write minimal implementation**

En `upload.py`, imports y constantes al principio pasan a ser:

```python
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import tracklist

CONFIG_DIR = Path.home() / ".config" / "tracklist"
UPLOADS_FILE = CONFIG_DIR / "uploads.json"
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "youtube_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded")
PROCESSING_POLL = 30
CLAIMS_POLL = 120
CLAIMS_STABLE_POLLS = 3
CLAIMS_TIMEOUT = 3600
```

y añadir al final:

```python
def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_uploads():
    if not UPLOADS_FILE.exists():
        return {}
    return json.loads(UPLOADS_FILE.read_text())


def known_upload(path):
    return load_uploads().get(file_hash(path))


def remember_upload(path, video_id):
    uploads = load_uploads()
    uploads[file_hash(path)] = video_id
    UPLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_FILE.write_text(json.dumps(uploads))


def youtube_client():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit(
            "Para subir audio ejecuta con el python del venv.\n"
            "Dependencias (una vez): .venv/bin/pip install google-api-python-client google-auth-oauthlib"
        )
    if not CLIENT_SECRET.exists():
        sys.exit(
            "Falta la credencial de YouTube. Una sola vez:\n"
            "  1. console.cloud.google.com > proyecto nuevo\n"
            "  2. APIs y servicios > activa 'YouTube Data API v3'\n"
            "  3. Pantalla de consentimiento OAuth > externo, anadete como usuario de prueba\n"
            "  4. Credenciales > ID de cliente OAuth > 'App de escritorio' > descargar JSON\n"
            f"  5. Guardalo como {CLIENT_SECRET}"
        )
    credentials = None
    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        credentials = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(credentials.to_json())
    TOKEN_FILE.chmod(0o600)
    return build("youtube", "v3", credentials=credentials)


def upload_video(client, path, title):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {"snippet": {"title": title}, "status": {"privacyStatus": "private"}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=-1, resumable=True)
    request = client.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        response = None
        while response is None:
            _, response = request.next_chunk()
    except HttpError as error:
        if any(reason in error.content.decode() for reason in QUOTA_REASONS):
            sys.exit("Cuota de subidas de YouTube agotada: prueba manana")
        raise
    return response["id"]


def prepare(path, report):
    client = youtube_client()
    video_id = known_upload(path)
    if video_id:
        report(f"Ya subido como https://youtu.be/{video_id}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / f"{path.stem}.mp4"
            report("Convirtiendo a video")
            convert_to_video(path, video)
            report("Subiendo a YouTube")
            video_id = upload_video(client, video, path.stem)
        remember_upload(path, video_id)
        report(f"Subido como https://youtu.be/{video_id}")
    wait_for_processing(client, video_id, report)
    wait_for_claims(video_id, report)
    return video_id
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add upload.py test_tracklist.py
git commit -m "Upload a local audio file to YouTube once and wait for its claims"
```

---

### Task 4: `tracklist fichero.m4a` en la línea de comandos

**Files:**
- Modify: `tracklist.py` (`build_parser`, `main`, nueva `local_file` junto a `parse_video_id`)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `upload.prepare(path, report) -> str`, `upload.place_audio(source, video_id)`.
- Produces: `tracklist.local_file(text: str) -> Path | None`.

- [ ] **Step 1: Write the failing tests**

Añadir antes de `if __name__ == "__main__":`:

```python
class LocalFileTest(unittest.TestCase):
    def test_an_existing_recording_takes_the_upload_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Spook Multiespacio 3.m4a"
            source.write_bytes(b"audio")
            self.assertEqual(tracklist.local_file(str(source)), source)

    def test_a_video_id_keeps_the_youtube_path(self):
        self.assertIsNone(tracklist.local_file("vid12345678"))

    def test_a_video_url_keeps_the_youtube_path(self):
        self.assertIsNone(tracklist.local_file("https://www.youtube.com/watch?v=vid12345678"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -20`
Expected: ERROR `AttributeError: module 'tracklist' has no attribute 'local_file'`.

- [ ] **Step 3: Write minimal implementation**

En `tracklist.py`, después de `parse_video_id`:

```python
def local_file(text):
    path = Path(text).expanduser()
    return path if path.is_file() else None
```

En `build_parser`, cambiar la ayuda del posicional:

```python
    parser.add_argument("video", nargs="?", help="URL o ID del video, o fichero de audio")
```

En `main`, cambiar el prompt interactivo:

```python
        print("URL o ID del video, o fichero de audio: ", end="", file=sys.stderr, flush=True)
```

y sustituir:

```python
    video_id = parse_video_id(args.video)
    if args.fresh:
        clear_cache(video_id)
```

por:

```python
    local = local_file(args.video)
    if local:
        import upload

        video_id = upload.prepare(local, lambda message: print(message, file=sys.stderr))
    else:
        video_id = parse_video_id(args.video)
    if args.fresh:
        clear_cache(video_id)
    if local:
        upload.place_audio(local, video_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest test_tracklist -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Manual verification con la API real**

Requiere que Vicent haya creado `~/.config/tracklist/client_secret.json` (el propio comando le explica cómo si falta) y autorice en el navegador.

Run: `.venv/bin/python tracklist.py ~/Downloads/spook/"Spook Multiespacio 3.m4a"`
Expected:
- stderr: `Convirtiendo a video`, `Subiendo a YouTube`, `Subido como https://youtu.be/<id>`, `Procesando en YouTube (...)`, luego `Reclamaciones: N (mm:ss)` cada 2 min.
- El vídeo aparece privado en Studio con título `Spook Multiespacio 3`.
- `~/.config/tracklist/uploads.json` contiene el hash → `<id>`; `~/.config/tracklist/youtube_token.json` tiene permisos `-rw-------`.
- Al terminar la espera se abre el flujo normal (TUI) y no hay `download start` (el audio sale de la caché local).

Después, interrumpir con Ctrl-C durante la espera de un segundo lanzamiento y relanzar: debe imprimir `Ya subido como ...` y no subir de nuevo.

- [ ] **Step 6: Commit**

```bash
git add tracklist.py test_tracklist.py
git commit -m "Accept a local audio file and analyse it through a YouTube upload"
```
