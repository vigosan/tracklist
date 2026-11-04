# Tracklist para cualquiera — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que `tracklist <URL ajena>` descargue, suba y saque el tracklist; que no haga falta capturar cURL ni usar Brave; y que cualquiera lo instale desde GitHub y la primera vez se le guíe para crear su credencial de Google.

**Architecture:** Se mantienen los cuatro módulos planos (`tracklist.py`, `upload.py`, `recognize_ui.py`, `recognize_tui.py`). La sesión de Studio se construye desde cookies del navegador guardado en `~/.config/tracklist/config.json`. El camino de vídeo ajeno vive en `upload.py` y desemboca en el `prepare` existente. Empaquetado con `pyproject.toml` y entry point.

**Tech Stack:** Python 3.13+, unittest (ejecutado con pytest), yt-dlp, ffmpeg, google-api-python-client, google-auth-oauthlib, shazamio, textual.

**Spec:** `docs/superpowers/specs/2026-11-04-public-release-design.md`

## Global Constraints

- Mensajes de usuario en español, sin tildes en el código como el resto de mensajes existentes (estilo actual: "sesion", "video").
- Cero comentarios en el código. Tests con nombres que explican el porqué (estilo actual: `test_a_renamed_file_is_still_recognised_by_its_content`).
- Tests con `unittest.TestCase` en `test_tracklist.py`, dependencias sustituidas asignando atributos del módulo y restaurándolos con `addCleanup` (patrón de `PrepareTest`, `IsolatedCache`, `IsolatedUploads`).
- Test runner: `.venv/bin/python -m pytest -q` debe terminar con 0 fallos tras cada tarea.
- `STUDIO_CLIENT_VERSION = "1.20260805.02.02"`.
- Opciones de descarga verbatim: `-N 4 --throttled-rate 100K --retries 10 --fragment-retries 10 --limit-rate 5M -f bestaudio`.
- Subida siempre `privacyStatus: "private"` (ya es así).
- Commits: solo título descriptivo, sin prefijos, sin cuerpo. Un commit por tarea.

## Review Focus

1. Ningún navegador instalado tiene sesión de YouTube → mensaje que dice que inicie sesión en studio.youtube.com, sin traza. (Task 1, `test_no_browser_with_a_youtube_session_explains_what_to_do`)
2. El navegador guardado ya no tiene sesión → el mensaje nombra ese navegador y `--browser`. (Task 1, `test_a_saved_browser_that_lost_its_session_names_it_and_the_way_out`)
3. Relanzar la misma URL ajena tras cortar la espera de Content ID → ni re-descarga ni re-subida. (Task 4, `test_relaunching_a_foreign_url_neither_downloads_nor_uploads_again`)
4. Vídeo ajeno que yt-dlp no puede descargar (privado, borrado, geobloqueado) → aviso y sigue con solo Shazam sobre el original, sin traza. (Task 4, `test_a_video_that_cannot_be_downloaded_falls_back_to_shazam`)
5. En el asistente, el usuario pega la ruta de un JSON que no es un cliente de escritorio, o en `~/Downloads` hay un `client_secret` viejo de un intento anterior → se rechaza / se ignora y se vuelve a pedir. (Task 6, `test_a_json_that_is_not_a_desktop_client_is_rejected`, `test_an_old_client_secret_in_downloads_is_ignored`)

---

## Parte 1 — Sesión sin cURL y cualquier navegador

### Task 1: Navegador detectado y guardado

**Files:**
- Modify: `tracklist.py` (`brave_cookies` → `browser_cookies`; nuevas `installed_browsers`, `pick_browser`, `current_browser`, `save_browser`; `probe_duration` y `download_audio` usan `current_browser()`; `build_parser` añade `--browser`; `main` guarda el navegador si viene el flag)
- Test: `test_tracklist.py`

**Interfaces:**
- Produces:
  - `BROWSERS: dict[str, str]` nombre yt-dlp → nombre de la app, en este orden: `chrome: "Google Chrome.app"`, `brave: "Brave Browser.app"`, `edge: "Microsoft Edge.app"`, `firefox: "Firefox.app"`, `safari: "Safari.app"`, `vivaldi: "Vivaldi.app"`, `opera: "Opera.app"`, `chromium: "Chromium.app"`.
  - `APP_DIRS = [Path("/Applications"), Path.home() / "Applications"]`
  - `CONFIG_FILE = Path.home() / ".config" / "tracklist" / "config.json"`
  - `browser_cookies(browser: str) -> dict[str, str]` (mismo cuerpo que `brave_cookies`, navegador parametrizado; los `sys.exit` dejan de mencionar Brave y `--save-session`; si stderr contiene "Operation not permitted" y el navegador es safari, el mensaje dice que Terminal necesita "Acceso total al disco" en Ajustes > Privacidad).
  - `installed_browsers(app_dirs: list[Path]) -> list[str]` en orden de `BROWSERS`.
  - `pick_browser(candidates: list[str], read_cookies) -> str | None`: primero cuyo `read_cookies(nombre)` contiene `"SAPISID"`; un `SystemExit` de `read_cookies` cuenta como "sin sesión" y se pasa al siguiente.
  - `current_browser() -> str`: lee `CONFIG_FILE["browser"]`; si no hay, `pick_browser(installed_browsers(APP_DIRS), browser_cookies)`, lo guarda con `save_browser` y lo devuelve; si es `None`, `sys.exit("Ningun navegador tiene sesion de YouTube: inicia sesion en studio.youtube.com y reintenta.")`.
  - `save_browser(browser: str) -> None`: escribe/actualiza `CONFIG_FILE` como JSON conservando otras claves.

- [ ] **Step 1: Tests que fallan** — clase `BrowserTest` con `CONFIG_FILE` y `APP_DIRS` aislados en un `TemporaryDirectory`:
  - `test_only_installed_browsers_are_tried_so_macos_does_not_ask_for_every_keychain`: crear `Firefox.app` y `Google Chrome.app` en el dir temporal → `installed_browsers([dir]) == ["chrome", "firefox"]`.
  - `test_the_first_browser_signed_in_to_youtube_wins`: `pick_browser(["chrome", "firefox"], lambda b: {"SAPISID": "x"} if b == "firefox" else {})` → `"firefox"`.
  - `test_a_browser_whose_cookies_cannot_be_read_is_skipped`: `read_cookies` lanza `SystemExit` para chrome y devuelve sesión para firefox → `"firefox"`.
  - `test_the_detected_browser_is_remembered_instead_of_probed_every_run`: `current_browser()` con `browser_cookies` sustituido por un contador; segunda llamada no llama a `browser_cookies`; `CONFIG_FILE` contiene `{"browser": ...}`.
  - `test_no_browser_with_a_youtube_session_explains_what_to_do`: sin apps → `SystemExit` cuyo código contiene `"studio.youtube.com"`.
  - `test_browser_flag_replaces_the_saved_choice`: `save_browser("chrome")` tras `{"browser": "brave", "otra": 1}` → `{"browser": "chrome", "otra": 1}`.
  - `test_yt_dlp_reads_cookies_from_the_saved_browser`: extender `ProbeDurationTest` existente: con `current_browser` sustituido por `lambda: "firefox"`, el comando contiene `["--cookies-from-browser", "firefox"]`.
- [ ] **Step 2:** `.venv/bin/python -m pytest -q -k "BrowserTest or ProbeDuration"` → FAIL (`AttributeError`).
- [ ] **Step 3:** Implementar las funciones de arriba. `--browser` en `build_parser` con `choices=list(BROWSERS)`; en `main`, si viene, `save_browser(args.browser)` antes de todo lo demás.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos.
- [ ] **Step 5:** `git commit -m "Read YouTube cookies from whichever browser is signed in, not only Brave"`

### Task 2: Sesión de Studio sin cURL

**Files:**
- Modify: `tracklist.py` (nueva `studio_session`; `fresh_session` reescrita; `post` con mensaje para 404 y 401/403; eliminar `parse_curl`, `save_session`, `load_session`, `refresh_headers`, `SESSION_FILE`, `DROP_HEADERS`, `--save-session`, imports que queden sin uso como `shlex`)
- Modify: `upload.py` (`wait_for_claims`: mensaje de sesión ausente)
- Test: `test_tracklist.py` (borrar `RefreshHeadersTest` y cualquier test de `parse_curl`)

**Interfaces:**
- Consumes: `current_browser()`, `browser_cookies(browser)` de Task 1; `build_authorization(cookies, timestamp)` existente.
- Produces:
  - `STUDIO_CLIENT_VERSION = "1.20260805.02.02"`
  - `studio_session(cookies: dict, timestamp: int) -> {"headers": dict, "context": dict}` con headers exactamente `cookie`, `authorization`, `origin`, `x-origin` (ambos `ORIGIN`), `x-youtube-client-name: "62"`, `x-youtube-client-version: STUDIO_CLIENT_VERSION`; context `{"client": {"clientName": 62, "clientVersion": STUDIO_CLIENT_VERSION}}`.
  - `fresh_session()`: `browser = current_browser()`, cookies; si falta `SAPISID` → `sys.exit(f"{browser} ya no tiene sesion de YouTube: entra en studio.youtube.com con {browser} o elige otro con --browser.")`; devuelve `studio_session(cookies, int(time.time()))`.

- [ ] **Step 1: Tests que fallan** (`StudioSessionTest`):
  - `test_a_session_needs_only_browser_cookies_and_a_client_version`: `studio_session({"SAPISID": "a", "__Secure-1PAPISID": "b", "__Secure-3PAPISID": "c"}, 1700000000)` → set de claves de headers igual al listado, `authorization == build_authorization(cookies, 1700000000)`, context igual al de arriba.
  - `test_a_client_version_without_a_realistic_shape_is_never_sent`: `re.fullmatch(r"1\.\d{8}\.\d{2}\.\d{2}", STUDIO_CLIENT_VERSION)` (Studio responde 404 a `"1.0"`).
  - `test_a_saved_browser_that_lost_its_session_names_it_and_the_way_out`: `current_browser` → `"firefox"`, `browser_cookies` → `{}` → `SystemExit` con `"firefox"` y `"--browser"`.
  - `test_an_outdated_client_version_is_reported_as_such`: `urllib.request.urlopen` sustituido para lanzar `HTTPError` 404 → `post(...)` sale con mensaje que contiene `"STUDIO_CLIENT_VERSION"`.
  - En `WaitForClaimsTest.test_without_a_studio_session_it_stops_so_claims_are_not_skipped`, añadir aserción de que el mensaje contiene `"misma cuenta"`.
- [ ] **Step 2:** `.venv/bin/python -m pytest -q -k "StudioSession or WaitForClaims"` → FAIL.
- [ ] **Step 3:** Implementar. Mensajes: 401/403 en `post` → `"YouTube rechazo la sesion: entra en studio.youtube.com con la misma cuenta que usa tracklist y reintenta."`; 404 → `f"Studio respondio 404: si pasa con todos los videos, STUDIO_CLIENT_VERSION ({STUDIO_CLIENT_VERSION}) esta caducada."`; `wait_for_claims` sin sesión → `f"Studio no deja leer las reclamaciones de tu video: entra en studio.youtube.com con la misma cuenta con la que subiste (o cambia de navegador con --browser) y vuelve a lanzar el mismo comando (el video ya esta subido: https://youtu.be/{video_id})"`.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos. Prueba manual: `.venv/bin/python tracklist.py dnS4s0gar-4 --no-recognize` imprime 9 entradas sin usar `session.json` (renombrarlo temporalmente a `session.json.bak` y restaurarlo después).
- [ ] **Step 5:** `git commit -m "Build the Studio session from browser cookies instead of a captured cURL"`

## Parte 2 — Vídeos ajenos

### Task 3: Propiedad del vídeo y descarga del audio

**Files:**
- Modify: `upload.py`
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: `tracklist.current_browser()`, `tracklist.AUDIO_DIR`.
- Produces:
  - `DOWNLOAD_OPTIONS = ["-N", "4", "--throttled-rate", "100K", "--retries", "10", "--fragment-retries", "10", "--limit-rate", "5M", "-f", "bestaudio"]`
  - `downloads_dir() -> Path` = `tracklist.AUDIO_DIR / "downloads"` (calculado en cada llamada para que `IsolatedCache` lo aísle).
  - `video_owner(client, video_id) -> tuple[bool, str | None]`: `(es_mio, titulo)`. `channels().list(part="id", mine=True)` vs `videos().list(part="snippet", id=video_id)`; sin items → `(False, None)`.
  - `downloaded_audio(video_id) -> Path | None`: fichero `downloads_dir()/<id>.*` que no acabe en `.part`.
  - `download_foreign(video_id) -> Path | None`: si `downloaded_audio` existe, lo devuelve sin llamar a yt-dlp; si no, `subprocess.run(["yt-dlp", "--quiet", "--no-warnings", "--progress", *DOWNLOAD_OPTIONS, "--cookies-from-browser", tracklist.current_browser(), "-o", str(downloads_dir() / f"{video_id}.%(ext)s"), url])` sin capturar salida (el usuario ve el progreso); returncode ≠ 0 → `None`.

- [ ] **Step 1: Tests que fallan** (`VideoOwnerTest` con un cliente falso de dos métodos encadenables; `DownloadForeignTest(IsolatedCache)` con `subprocess.run` sustituido):
  - `test_a_video_of_my_channel_is_mine`, `test_a_video_of_another_channel_is_not_mine_and_keeps_its_title`, `test_a_video_youtube_does_not_show_is_treated_as_foreign`.
  - `test_the_download_uses_the_users_yt_dlp_options_and_only_audio`: el comando contiene `DOWNLOAD_OPTIONS` como sublista contigua.
  - `test_an_audio_already_downloaded_is_reused`: crear `downloads/vid.webm` → no llama a `subprocess.run`.
  - `test_a_half_finished_download_is_not_mistaken_for_a_finished_one`: solo `vid.webm.part` → sí llama.
  - `test_a_failed_download_reports_nothing_to_upload`: returncode 1 → `None`.
- [ ] **Step 2:** `.venv/bin/python -m pytest -q -k "VideoOwner or DownloadForeign"` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos.
- [ ] **Step 5:** `git commit -m "Tell own videos from foreign ones and download a foreign one's audio"`

### Task 4: Subir el vídeo ajeno y caer a Shazam si no se puede

**Files:**
- Modify: `upload.py` (`QuotaExceeded`, `upload_video`, `prepare(path, report, title=None)`, nuevas `remote_key`, `adopt`, `forget_upload` borra la descarga)
- Modify: `tracklist.py` (`main`)
- Test: `test_tracklist.py`

**Interfaces:**
- Consumes: Task 3 entera; `prepare`, `remember_upload`, `load_uploads` existentes.
- Produces:
  - `class QuotaExceeded(Exception)`; `upload_video` la lanza en vez de `sys.exit`. `main` (camino de fichero local) la convierte en `sys.exit("Cuota de subidas de YouTube agotada: prueba manana")`.
  - `prepare(path, report, title=None)`: el título de la subida es `title or path.stem`.
  - `remote_key(video_id) -> str` = `f"youtube:{video_id}"`; `uploads.json` guarda `remote_key(original) → subido` junto a los hashes existentes.
  - `adopt(video_id, report) -> tuple[str, Path | None, str | None]` = `(id_a_analizar, audio_local, titulo_original)`:
    1. `client = youtube_client()`; `SystemExit` → aviso y `(video_id, None, None)`.
    2. `video_owner` → propio: `(video_id, None, None)`.
    3. Ajeno: `download_foreign`; `None` → `report("No se pudo descargar el video: sigo solo con Shazam")` y `(video_id, None, title)`.
    4. Si `load_uploads()` ya tiene `remote_key(video_id)` se reutiliza ese id (sin re-subir: `prepare` ya lo detecta por hash; además registrar la clave remota tras la primera subida).
    5. `prepare(audio, report, title)`; `QuotaExceeded` → `report("Cuota de subidas de YouTube agotada: sigo solo con Shazam")` y `(video_id, None, title)`.
    6. `remember` de `remote_key(video_id) → subido`; devuelve `(subido, audio, title)`.
  - `forget_upload(video_id)`: además de lo actual, borra `downloads_dir()/<original>.*` de cada `youtube:<original>` que apuntaba a `video_id`.
  - `main`: para URL/ID, `video_id, audio, original_title = upload.adopt(original_id, report)`; si `audio`, `place_audio(audio, video_id)` y duración con `local_duration(audio)`; el `render` final usa `original_id` y `original_title or title` (en ambos caminos: TUI y no TUI).

- [ ] **Step 1: Tests que fallan** (`AdoptTest(IsolatedUploads, IsolatedCache)` con `youtube_client`, `video_owner`, `download_foreign`, `prepare` sustituidos):
  - `test_my_own_video_is_read_directly_without_uploading`
  - `test_a_foreign_video_is_uploaded_with_its_original_title`
  - `test_relaunching_a_foreign_url_neither_downloads_nor_uploads_again`: dos `adopt` con `prepare`/`download_foreign` reales sustituidos solo en `convert_to_video`, `upload_video`, `wait_*`, `subprocess.run` → un solo `upload_video` y una sola llamada a yt-dlp.
  - `test_without_a_google_login_it_keeps_working_with_shazam_alone`: `youtube_client` lanza `SystemExit` → `("orig", None, None)`.
  - `test_a_video_that_cannot_be_downloaded_falls_back_to_shazam`
  - `test_an_exhausted_quota_falls_back_to_shazam_instead_of_stopping`
  - `test_deleting_the_uploaded_copy_also_deletes_the_download`: tras `adopt`, `forget_upload(subido)` → sin fichero en `downloads_dir()` y sin clave `youtube:` en `uploads.json`.
  - `test_the_tracklist_points_to_the_original_video_not_the_copy`: `render(entries, gaps, "orig", False, "Set")` ya lo hace; el test va en `main`: sustituir `upload.adopt` → `("copia", None, "Set")`, `try_claims`/`recognize`, capturar stdout y comprobar `watch?v=orig` y ausencia de `copia`.
  - Actualizar `PrepareTest.test_a_failed_upload_is_retried_on_the_next_run` para usar `upload.QuotaExceeded()`.
- [ ] **Step 2:** `.venv/bin/python -m pytest -q -k "Adopt or Prepare"` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos. Prueba manual de extremo a extremo: `.venv/bin/python tracklist.py "https://www.youtube.com/watch?v=tZ6YRoNjvPI"`; anotar en el informe cuánto tarda cada fase y el número de reclamaciones; aceptar el borrado al final y comprobar que desaparece la descarga.
- [ ] **Step 5:** `git commit -m "Download and upload a foreign video to read its Content ID claims"`

### Task 5: Aviso en la TUI para URLs escritas dentro

**Files:**
- Modify: `recognize_tui.py` (`on_input_submitted` marca `self.typed = True`; `load` registra el aviso)
- Test: `test_tracklist.py`

**Interfaces:**
- Decisión del plan: la TUI no llama a la API oficial (evita abrir un login OAuth dentro de la pantalla). Se avisa cuando el vídeo se escribió dentro de la TUI y Studio no devolvió sesión (`try_claims` → `None`), que es lo que pasa con un vídeo ajeno.
- Texto exacto: `"[!] si el video no es tuyo, sal y lanza 'tracklist <url>' para subirlo y leer Content ID"`.

- [ ] **Step 1: Test que falla:** `test_a_foreign_url_typed_in_the_tui_explains_how_to_get_content_id` siguiendo el patrón de `RefreshKeyTest` (app con `try_claims` → `(None, [])`, `typed = True`, ejecutar `load`, la línea está en el log); y `test_the_hint_is_not_shown_for_the_video_tracklist_was_launched_with`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos.
- [ ] **Step 5:** `git commit -m "Explain in the TUI how to get Content ID for a foreign video"`

## Parte 3 — Asistente de credencial

### Task 6: Asistente de `client_secret.json`

**Files:**
- Modify: `upload.py` (nuevas `valid_client_secret`, `find_client_secret`, `setup_credentials`; `youtube_client` la llama si falta el fichero y hay TTY)
- Test: `test_tracklist.py`

**Interfaces:**
- Produces:
  - `valid_client_secret(path: Path) -> bool`: JSON legible con `installed.client_id`.
  - `find_client_secret(directory: Path, since: float) -> Path | None`: el `client_secret*.json` más reciente con `mtime >= since`.
  - `setup_credentials(open_url=webbrowser.open, ask=input, report=print_stderr, downloads=Path.home() / "Downloads", clock=time.time) -> None`.
- Pasos (texto en `report`, URL en `open_url`, `ask("Pulsa Enter para seguir")` entre cada uno):
  1. Intro: `"tracklist sube los videos a tu canal con tu propio proyecto de Google (gratis, una sola vez, ~5 minutos)."`
  2. `https://console.cloud.google.com/projectcreate` — "crea un proyecto (cualquier nombre)".
  3. `https://console.cloud.google.com/apis/library/youtube.googleapis.com` — "pulsa Habilitar".
  4. `https://console.cloud.google.com/auth/branding` — "rellena nombre y correo, tipo Externo".
  5. `https://console.cloud.google.com/auth/audience` — "pulsa 'Publicar app' para que el login no caduque cada 7 dias (veras un aviso de app no verificada: es tu propia app)".
  6. `https://console.cloud.google.com/auth/clients/create` — "tipo 'App de escritorio', crear y descargar el JSON".
  7. Bucle: `ask("Pulsa Enter cuando lo hayas descargado, o pega la ruta del JSON: ")`; vacío → `find_client_secret(downloads, inicio)`; texto → `Path(texto.strip().strip("'\"")).expanduser()`. Si no existe o `not valid_client_secret` → `report("Ese fichero no es un ID de cliente de 'App de escritorio'. Prueba otra vez.")` y repetir. Válido → copiar a `CLIENT_SECRET`, `chmod 0o600`.
- `youtube_client`: si falta `CLIENT_SECRET` y `sys.stdin.isatty() and sys.stderr.isatty()` → `setup_credentials()`; si no, el `sys.exit` actual con los pasos (actualizado a las mismas URLs).

- [ ] **Step 1: Tests que fallan** (`SetupCredentialsTest` con `CLIENT_SECRET` aislado y `ask` que consume una lista de respuestas):
  - `test_a_desktop_client_downloaded_during_the_wizard_is_installed`: el JSON aparece en `downloads` tras empezar → `CLIENT_SECRET` existe con permisos `0o600`.
  - `test_an_old_client_secret_in_downloads_is_ignored`: JSON con `mtime` anterior a `clock()` → se vuelve a preguntar.
  - `test_a_json_that_is_not_a_desktop_client_is_rejected`: ruta pegada a `{"web": {...}}` y luego a uno válido → se instala el segundo y el `report` contiene "App de escritorio".
  - `test_a_dragged_path_with_quotes_is_accepted`: `"'/ruta/client_secret.json' "`.
  - `test_every_step_opens_its_console_page`: `open_url` recibe las 5 URLs en orden.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos.
- [ ] **Step 5:** `git commit -m "Guide a new user through creating their Google credential"`

## Parte 4 — Instalable desde GitHub

### Task 7: Paquete, dependencias del sistema y README

**Files:**
- Create: `pyproject.toml`, `README.md`
- Modify: `tracklist.py` (shebang `#!/usr/bin/env python3`; nueva `missing_tools`; mensajes de `recognition_available`), `upload.py` (mensaje `ImportError`)
- Modify: `.gitignore` (añadir `docs/`, `sample.json`, `a.txt`, `.DS_Store`); `git rm --cached -r docs sample.json`
- Test: `test_tracklist.py`

**Interfaces:**
- `pyproject.toml`: build backend setuptools; `name = "tracklist"`; `requires-python = ">=3.13"`; dependencias `google-api-python-client`, `google-auth-oauthlib`, `shazamio==0.4.0.1`, `audioop-lts`, `textual`; `[project.scripts] tracklist = "tracklist:main"`; `[tool.setuptools] py-modules = ["tracklist", "upload", "recognize_ui", "recognize_tui"]`.
- `missing_tools(which=shutil.which) -> list[str]` sobre `["yt-dlp", "ffmpeg", "ffprobe"]`; `main` al empezar: si hay, `sys.exit(f"Falta {', '.join(...)}: brew install yt-dlp ffmpeg")`.
- Los mensajes que mandan a `.venv/bin/pip` pasan a: `"Reinstala tracklist: uv tool install --force git+https://github.com/vigosan/tracklist"`.
- README (español): qué hace; instalación (`brew install yt-dlp ffmpeg uv`, `uv tool install git+https://github.com/vigosan/tracklist`); primera ejecución (navegador con sesión en YouTube, asistente de credencial, login); uso (`tracklist <URL propia>`, `<URL ajena>`, `<fichero>`, `--browser`, `--fresh`, `--label`); límites (≈6 subidas al día por la cuota de cada proyecto de Google, Content ID tarda hasta una hora, vídeos ajenos se suben en privado y se ofrece borrarlos); datos guardados en `~/.config/tracklist` y `~/Library/Caches/tracklist`.

- [ ] **Step 1: Tests que fallan:** `test_missing_system_tools_are_named_so_the_user_knows_what_to_install` (`which` falso que no encuentra ffmpeg/ffprobe → `["ffmpeg", "ffprobe"]`); `test_the_installed_command_runs_main` (lee `pyproject.toml` con `tomllib` y comprueba `project.scripts.tracklist == "tracklist:main"` y que cada `py-modules` existe como fichero).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implementar todo lo de arriba.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → 0 fallos. Instalación limpia: `python3.14 -m venv $SCRATCH/venv && $SCRATCH/venv/bin/pip install . && $SCRATCH/venv/bin/tracklist --help` muestra la ayuda con `--browser` y sin `--save-session`. `git ls-files` no contiene `docs/` ni `sample.json`.
- [ ] **Step 5:** `git commit -m "Make tracklist installable from GitHub with a README"`

**Fuera de este plan:** crear el repo en GitHub y hacerlo público (lo hace Vicent). Recordar en el informe final que el historial aún contiene `docs/` y `sample.json`.
