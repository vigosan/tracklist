# Sesión automática desde Brave para tracklist

**Fecha:** 2026-08-10
**Estado:** aprobado por Vicent

## Problema

`tracklist.py` depende de una captura manual de sesión (DevTools → Copy as cURL →
`--save-session`). La captura caduca en horas/días porque la cabecera
`authorization: SAPISIDHASH` lleva el timestamp incrustado y porque Brave rota
las cookies, invalidando el snapshot. Verificado el 2026-08-10: la sesión del
2026-08-07 devuelve `401 UNAUTHENTICATED` incluso recalculando el hash, lo que
confirma que las cookies del snapshot también caducan.

## Objetivo

Ejecutar `tracklist <video>` y que funcione siempre, sin abrir DevTools nunca,
mientras exista una sesión de YouTube iniciada en Brave.

## Enfoque elegido

Leer las cookies frescas de Brave en cada ejecución vía `yt-dlp`
(ya instalado en `/opt/homebrew/bin/yt-dlp`) y recalcular `SAPISIDHASH` con el
timestamp actual. El `context` del body y el resto de cabeceras se reutilizan
del `session.json` capturado una vez con `--save-session`.

Alternativas descartadas: Playwright (dependencia pesada, Google bloquea logins
automatizados) y descifrar el SQLite de cookies a mano (reimplementa yt-dlp,
frágil ante cambios de cifrado de Chromium).

## Diseño

Flujo por ejecución (modo por defecto, sin flags nuevos):

1. Cargar `~/.config/tracklist/session.json` (cabeceras + `context`), como hoy.
2. Volcar cookies de Brave: subprocess a
   `yt-dlp --cookies-from-browser brave --cookies <tmp>`. Fichero temporal con
   permisos 600, borrado al terminar. La invocación exacta (URL dummy,
   `--skip-download`, etc.) se determina durante la implementación.
3. Parsear el fichero Netscape con `http.cookiejar.MozillaCookieJar` y construir
   la cabecera `Cookie` con las cookies de `.youtube.com` y
   `studio.youtube.com`.
4. Calcular `authorization` con timestamp actual:
   `SAPISIDHASH {ts}_{sha1("{ts} {SAPISID} https://studio.youtube.com")}` más
   las variantes `SAPISID1PHASH` (`__Secure-1PAPISID`) y `SAPISID3PHASH`
   (`__Secure-3PAPISID`).
5. Sobrescribir `cookie` y `authorization` sobre las cabeceras de
   `session.json` y hacer el POST igual que hoy.

`--save-session` y `--from-json` no cambian. `--save-session` sigue siendo el
respaldo si la vía automática falla.

## Errores

- `yt-dlp` ausente o falla (Llavero denegado, sin cookies de youtube.com):
  mensaje claro con el fallback manual `--save-session`.
- 401 con cookies frescas: "no hay sesión activa en Brave: entra en
  studio.youtube.com y vuelve a intentarlo".
- Resto de errores HTTP: comportamiento actual.

## Tests

`unittest` de la stdlib (sin dependencias nuevas):

- Cálculo de `SAPISIDHASH` con valores conocidos.
- Construcción de la cabecera `Cookie` desde un cookie jar.
- Merge de cabeceras (las frescas pisan a las guardadas).

La vía en vivo (yt-dlp + petición real) se verifica manualmente con el vídeo
`_6r0Kd41jfs`.

## Riesgos

- Primera ejecución: macOS pide autorizar acceso al Llavero
  ("Brave Safe Storage"). Se autoriza una vez.
- Si Google algún día rechaza el `context`/clientVersion antiguo del
  `session.json`, se recaptura una vez con `--save-session`.
