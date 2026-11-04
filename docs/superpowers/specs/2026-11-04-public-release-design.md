# Tracklist para cualquiera: vídeos ajenos, sin cURL, instalable desde GitHub

**Fecha:** 2026-11-04
**Estado:** aprobado por Vicent

## Por qué

Hoy tracklist solo sirve a Vicent: lee reclamaciones de vídeos de su canal,
depende de Brave, de un cURL capturado a mano en DevTools (`--save-session`),
de un `.venv` en su Escritorio y de un `client_secret.json` que creó él.

Dos deseos:

1. Pasar la URL de **cualquier** vídeo de YouTube (p. ej. un set de otro DJ)
   y que tracklist haga solo lo que Vicent hace a mano: descargarlo con
   yt-dlp, subirlo a su canal, esperar a Content ID, leer las reclamaciones
   y pasar Shazam por los huecos.
2. Que cualquiera que encuentre el repo en GitHub pueda instalarlo,
   ejecutarlo y que la primera vez se le pida iniciar sesión.

## Objetivos

- `tracklist <URL de vídeo ajeno>` produce el tracklist de principio a fin.
- `tracklist <URL de vídeo propio>` y `tracklist fichero.m4a` siguen igual.
- Un usuario nuevo llega al primer tracklist sin abrir DevTools ni copiar
  cURLs; solo crea su credencial de Google guiado por la herramienta e inicia
  sesión en el navegador.
- Funciona con el navegador que el usuario ya use, no solo Brave.

## No objetivos

- Credencial OAuth compartida en el repo / verificación de la app por Google
  (opción A descartada por ahora: la cuota de un proyecto, ~6 subidas/día,
  no da para todos los usuarios). Cada usuario usa su propio proyecto.
- Subir sin la API oficial (endpoints internos de subida).
- Linux/Windows: el objetivo es macOS (rutas como `~/Library/Caches` se
  mantienen). Si funciona en otros, bien, pero no se prueba.
- Traducir al inglés.

## Anti-objetivos

- Que un usuario nuevo tenga que leer código o abrir DevTools.
- Re-descargar o re-subir un vídeo que ya se procesó (gasta cuota: cada
  subida cuesta 1600 de 10 000 unidades/día).
- Que la detección de navegador dispare una ristra de diálogos del llavero
  de macOS en cada ejecución.

## Hallazgos que fundamentan el diseño (spike del 2026-11-04)

Probado contra la API interna de Studio con cookies reales de Brave:

- `list_creator_received_claims` y `get_creator_videos` responden igual que
  con la sesión capturada usando **solo**: cabeceras `cookie`,
  `authorization` (SAPISIDHASH ya implementado en `build_authorization`),
  `origin`, `x-origin`, `x-youtube-client-name: 62`,
  `x-youtube-client-version`, y un `context` mínimo
  `{"client": {"clientName": 62, "clientVersion": <versión>}}`.
  No hacen falta `sessionInfo.token`, `x-goog-authuser`, ni el ID de canal.
- La versión debe tener forma realista: `1.20260805.02.02` y
  `1.20240101.00.00` funcionan; `1.0` o ausente → 404.
- Con un vídeo ajeno (`tZ6YRoNjvPI`): reclamaciones → **403** "The caller
  does not have permission"; `get_creator_videos` sí devuelve el vídeo
  (no sirve para saber si es propio). El 403 es ambiguo con una sesión
  rechazada, que hoy `post` también trata como 403.

## Diseño

Cuatro piezas, en este orden, cada una un commit (o varios) independiente
y con la herramienta funcionando entre medias.

### 1. Sesión de Studio sin cURL y con cualquier navegador

- `fresh_session()` construye la sesión desde cero con las cookies del
  navegador y las cabeceras/contexto mínimos de arriba. La versión es una
  constante `STUDIO_CLIENT_VERSION = "1.20260805.02.02"`.
- Desaparecen `--save-session`, `parse_curl`, `save_session`,
  `load_session`, `SESSION_FILE`, `DROP_HEADERS` y sus tests. El
  `session.json` existente se ignora (no se borra).
- Si Studio responde 404 a una petición bien formada, el mensaje de error
  dice que la versión de Studio parece caducada (para que quien mantenga
  el repo actualice la constante).
- **Navegador:** se guarda en `~/.config/tracklist/config.json`
  (`{"browser": "chrome"}`). Si no hay ninguno guardado, se prueban los
  navegadores **instalados** (comprobando su app en `/Applications` o
  `~/Applications`) en este orden: Chrome, Brave, Edge, Firefox, Safari, Vivaldi, Opera,
  Chromium — solo navegadores que acepta `yt-dlp --cookies-from-browser`
  (Arc no está soportado por yt-dlp y queda fuera). Se
  elige el primero cuyas cookies de `youtube.com` tengan `SAPISID` y se
  guarda. Si ninguno tiene sesión: mensaje "inicia sesión en
  studio.youtube.com en tu navegador y reintenta". Safari necesita acceso
  total al disco para Terminal: si falla por permisos, decirlo.
- `--browser NOMBRE` fuerza y guarda otro navegador.
- **Cuenta distinta:** si el navegador tiene sesión con otra cuenta o canal
  que el login de Google, Studio responde 403 a las reclamaciones de un
  vídeo que la API oficial dice que es propio. En ese caso el mensaje lo
  dice explícitamente ("el navegador tiene abierta otra cuenta de YouTube;
  cambia de cuenta en studio.youtube.com o usa --browser"), en vez del
  genérico "Sin sesión de Studio".
- Todas las llamadas a yt-dlp que hoy llevan `--cookies-from-browser brave`
  (`brave_cookies`, `probe_duration`, `download_audio`) usan el navegador
  guardado. `brave_cookies` pasa a llamarse según el navegador genérico.
- Los mensajes que mencionan Brave o `--save-session` se actualizan.

### 2. Vídeos ajenos: descargar y subir

- Para una URL/ID, se decide si el vídeo es del usuario con la API oficial:
  `channels.list(mine=True)` vs `videos.list(part="snippet", id=...)`
  `.snippet.channelId` (2 unidades de cuota). No se usa el 403 de Studio por
  ser ambiguo. Consecuencia aceptada: con URL también hace falta el login
  de Google.
- **Propio:** flujo actual sin cambios.
- **Ajeno:** se descarga **solo el audio** con las opciones de Vicent:
  `yt-dlp -N 4 --throttled-rate 100K --retries 10 --fragment-retries 10
  --limit-rate 5M -f bestaudio` (más `--cookies-from-browser` del navegador
  guardado, para vídeos con restricción de edad) a
  `~/Library/Caches/tracklist/downloads/<id_original>.<ext>`. Se muestra
  progreso de descarga (puede tardar minutos en sets largos).
- Luego se entra en el camino existente de fichero local
  (`upload.prepare`, `place_audio`, `local_duration`), subiendo en
  **privado** (ya es así) con el **título del vídeo original**.
- **Reanudar:** volver a lanzar la misma URL no re-descarga ni re-sube.
  `uploads.json` recuerda también `id_original → id_subido`; si la descarga
  ya existe en disco se reutiliza.
- **Salida:** el tracklist muestra el título y la URL del **vídeo
  original**, no los de la copia subida.
- **Si no se puede subir** (cuota agotada, sin login de Google, sin
  terminal interactiva para el asistente): aviso de por qué y se sigue con
  solo Shazam sobre el vídeo original, como hace hoy con un vídeo ajeno.
- **En la TUI** (URL nueva escrita dentro de la pantalla interactiva): una
  URL ajena no se descarga ni se sube; sigue con solo Shazam y muestra
  "para subirlo, sal y lanza `tracklist <url>`".
- Al aceptar borrar el vídeo subido (`offer_delete`), se borra también la
  descarga y la entrada `id_original` de `uploads.json`.

### 3. Asistente de credencial de Google

Cuando falta `~/.config/tracklist/client_secret.json` y hay terminal
interactiva, en lugar del mensaje actual:

1. Explica en una línea qué va a pasar y por qué (cada usuario usa su
   propio proyecto de Google; es gratis y se hace una vez).
2. Guía paso a paso, abriendo cada página con `webbrowser.open` y esperando
   Enter entre pasos: crear proyecto → activar YouTube Data API v3 →
   pantalla de consentimiento (externa, añadirse como usuario de prueba) →
   crear ID de cliente OAuth "App de escritorio" → descargar JSON.
3. Detecta el JSON: vigila `~/Downloads` por un `client_secret*.json` más
   nuevo que el inicio del asistente, o acepta una ruta pegada/arrastrada.
   Lo valida (tiene `installed.client_id`) y lo copia a su sitio con
   permisos 600.
4. Sigue con el login OAuth que ya existe (`run_local_server`).

Sin terminal interactiva se mantiene un mensaje con los pasos.

Nota para el builder: en modo "prueba" Google caduca el refresh token a los
7 días; el código ya vuelve a pedir login cuando pasa (`RefreshError`).
Verificar si conviene indicar en el asistente "Publicar app" para evitarlo
(app sin verificar con un solo usuario) y documentar lo que se decida.

### 4. Instalable desde GitHub

- `pyproject.toml` con los módulos actuales (`tracklist`, `upload`,
  `recognize_ui`, `recognize_tui`) como `py-modules`, entry point
  `tracklist = "tracklist:main"`, `requires-python` acorde a
  `audioop-lts` (3.13+), y dependencias: `google-api-python-client`,
  `google-auth-oauthlib`, `shazamio==0.4.0.1`, `audioop-lts`, `textual`.
- Instalación documentada: `brew install yt-dlp ffmpeg uv` y
  `uv tool install git+https://github.com/vigosan/tracklist`.
- El shebang que apunta a `/Users/vicent/Desktop/tracklist/.venv` y los
  mensajes que mandan a `.venv/bin/pip` se quitan o se reescriben: con el
  paquete instalado las dependencias siempre están.
- Al arrancar, si falta `yt-dlp` o `ffmpeg`, se dice cómo instalarlos.
- `README.md` en español: qué hace, instalación, primera ejecución
  (navegador + asistente de credencial), uso con URL propia, ajena y
  fichero, límites (≈6 subidas/día por la cuota de cada usuario; Content ID
  puede tardar hasta una hora) y qué datos guarda en `~/.config/tracklist`.
- Antes de publicar se quitan del repo `sample.json` (reclamaciones reales,
  ningún test lo usa) y `docs/` (se quedan en local, ignorados por git). El
  historial de git no se reescribe. Crear el repo en GitHub y hacerlo
  público lo hace Vicent, no el builder.

## Decisiones tomadas por Claude (revisar)

- Idioma: todo sigue en español.
- Descargar solo audio en vez del vídeo completo.
- Orden y lista de navegadores (sin Arc); guardar el elegido; flag `--browser`.
- Detectar y explicar el caso de cuentas distintas navegador/OAuth.
- Propiedad del vídeo vía Data API en vez del 403 de Studio.
- Título/URL del vídeo original en la salida.
- Borrar la descarga junto con el vídeo subido.
- `uv tool install` como forma de instalación.

## Para el builder

- Estructura interna, nombres de funciones nuevas y reparto entre módulos.
- Detalle del mensaje de progreso de descarga (la TUI vs stderr).
- Cómo se testea la detección de navegador y el asistente sin red ni
  navegador real (inyectar dependencias como ya hacen los tests actuales).

## Contexto del repo

- Hay cambios sin commitear en `tracklist.py`/`test_tracklist.py`
  (fusión de muestras de Shazam, `MERGE_WINDOW_SECONDS`) que no son parte
  de esto: commitearlos o apartarlos antes de empezar.
- El repo no tiene remoto configurado.
