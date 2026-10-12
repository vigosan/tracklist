# Subir un audio local a YouTube para sacar su tracklist

**Fecha:** 2026-10-12
**Estado:** aprobado por Vicent

## Problema

Hay sesiones grabadas como audio local (`.m4a`) que no están en YouTube.
Content ID reconoce muchas más pistas que Shazam, pero solo actúa sobre
vídeos subidos al canal, y YouTube no acepta audio suelto.

## Objetivo

`tracklist fichero.m4a` convierte el audio en vídeo, lo sube como privado,
espera a que Content ID lo reclame y saca el tracklist con el flujo actual
(reclamaciones + Shazam en los huecos), en un solo comando y reanudable.

## Diseño

### Entrada

En `main`, si `args.video` es la ruta de un fichero existente se toma el
camino de subida; si no, se interpreta como URL/ID igual que hoy. El camino
de subida resuelve un `video_id` y a partir de ahí el flujo es el actual.

### Reanudar

`~/.config/tracklist/uploads.json` guarda `sha256 del fichero → video_id`.
Si el hash ya está, no se convierte ni se sube: se pasa directamente a la
espera. El `video_id` se escribe nada más terminar la subida, antes de
esperar, para que un corte durante la espera no provoque otra subida.

### Convertir

ffmpeg genera un `.mp4` temporal: vídeo negro 640x360 a 1 fps desde
`lavfi color`, audio copiado sin recodificar (`-c:a copy`), `-shortest`.

### Subir

- YouTube Data API v3, `videos.insert` con subida resumable.
- Título: nombre del fichero sin extensión. `privacyStatus: private`.
- OAuth de aplicación instalada: `~/.config/tracklist/client_secret.json`
  (lo crea el usuario una vez en Google Cloud). La primera vez abre el
  navegador; el token se guarda en `~/.config/tracklist/youtube_token.json`
  con permisos 600 y se refresca solo.
- Scope: `https://www.googleapis.com/auth/youtube.upload` y
  `https://www.googleapis.com/auth/youtube.readonly` (estado de procesado).
- Dependencias nuevas en el venv: `google-api-python-client`,
  `google-auth-oauthlib`. Se importan solo en el camino de subida; si
  faltan, se sale con el comando de instalación, como ya se hace con
  shazamio.

### Audio para Shazam

Antes de lanzar el reconocimiento, el fichero local se convierte a mp3 en
`audio_path(video_id)`. El reconocedor ya reutiliza ese fichero si existe
(`("download", "cached")`), así que no se descarga nada con yt-dlp y el
código de reconocimiento no cambia.

### Esperar

Se ejecuta antes de abrir el TUI, informando por stderr de la fase y el
tiempo transcurrido.

1. Procesado: `videos.list(part=processingDetails)` cada 30 s hasta
   `processingStatus` = `succeeded` (o `failed` → error).
2. Reclamaciones: `fetch_claims` con la sesión de Studio existente cada
   2 minutos. Se considera estable cuando el número de reclamaciones no
   cambia en 3 consultas seguidas **y** es mayor que cero, o al llegar a
   60 minutos desde el fin del procesado.

La decisión de estabilidad es una función pura
(`claims_settled(counts, elapsed)`) para poder probarla sin red.

Limitación conocida: YouTube no expone cuándo Content ID ha terminado; el
criterio de estabilidad es una aproximación.

### Después

Se lanza el flujo actual con el `video_id` (TUI si está disponible). El
vídeo se queda privado en el canal; no se borra, para poder reconsultarlo
con `tracklist <id>`.

## Errores

- Falta `client_secret.json`: salir explicando cómo crearlo (proyecto en
  Google Cloud, activar YouTube Data API v3, credencial OAuth de escritorio,
  descargar a la ruta indicada).
- Cuota agotada (`quotaExceeded`/`uploadLimitExceeded`): salir con mensaje
  claro; el fichero no queda registrado en `uploads.json`.
- Procesado fallido: salir con el estado que devuelve la API.
- Tiempo de espera agotado: continuar con lo que haya y avisar de que
  `tracklist <id>` más tarde puede encontrar más reclamaciones.

## Tests (TDD, sin red)

- `main` distingue fichero existente de URL/ID.
- Un fichero ya registrado en `uploads.json` no se vuelve a subir.
- Tras subir, el `video_id` queda registrado con el hash del fichero.
- El comando ffmpeg de conversión a vídeo copia el audio y usa fondo negro.
- `claims_settled`: estable tras 3 recuentos iguales > 0; no estable con
  ceros; se rinde a los 60 minutos.
- El audio local acaba en `audio_path(video_id)` antes del reconocimiento.

La API de Google y la subida real se prueban a mano con
`Spook Multiespacio 3.m4a` (460 KB).

## Fuera de alcance

Subida en lote, borrar los vídeos subidos, portada personalizada.
