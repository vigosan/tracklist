# Descarga única del audio en vez de tramo por hueco

**Fecha:** 2026-08-12
**Estado:** aprobado por Vicent

## Problema

`download_gap` usa `yt-dlp --download-sections` una vez por hueco. Ese modo
de extracción parcial es lentísimo: medido en este proyecto, descargar un
solo tramo de 15 minutos de audio tardó **479 s (8 min)** para 14 MB. Con 7
huecos, la descarga domina la ejecución (~30-40 min) y esconde por completo
la ganancia del reconocimiento en paralelo.

## Medición que motiva el cambio

| Operación | Tiempo | Tamaño |
|---|---|---|
| 1 tramo de 15 min con `--download-sections` | 479 s | 14 MB |
| Audio completo (1h03m) sin `--download-sections` | **28 s** | 64 MB |
| Cortar 5 muestras del fichero completo con ffmpeg | < 1 s | — |

## Objetivo

Descargar el audio del vídeo una sola vez y cortar todas las muestras de
ese fichero local.

## Diseño

### Descarga

`download_gap(video_id, start, end, directory)` se sustituye por
`download_audio(video_id, directory)`, que baja el audio completo:

```
yt-dlp --quiet --no-warnings -x --audio-format mp3
       --cookies-from-browser brave -o <directory>/audio.%(ext)s <url>
```

Devuelve la ruta del fichero, o `None` si falla (con aviso por stderr,
igual que hoy).

### Bucle

`recognize_gaps_async` descarga una vez antes del bucle de huecos. Si la
descarga falla, se emite `("done", 0, segundos)` y se devuelve lista vacía
(no hay nada que reconocer). Dentro del bucle, cada hueco conserva su
evento `("gap", ...)` y su lógica de muestras; lo único que cambia es que
`cut_sample` recibe el offset **absoluto** (`offset`) en vez del relativo
al tramo (`offset - start`), porque el fichero ya es el vídeo entero.

La pausa antes de la descarga se mantiene (ahora antes de la descarga
única). El resto del contrato de eventos no cambia.

### Consecuencias aceptadas

- Ocupa ~64 MB de disco temporal durante la ejecución, dentro del
  `TemporaryDirectory` que ya se borra al terminar.
- En un vídeo con un único hueco corto se descarga de más, pero sigue
  saliendo a cuenta: 28 s de descarga completa frente a minutos por tramo.
- Desaparece el aviso "no se pudo descargar el hueco X"; pasa a ser un
  único aviso de descarga fallida.

## Errores

- Fallo de descarga: aviso por stderr y ejecución sin sugerencias (no
  aborta el programa; el tracklist de Content ID se imprime igual).
- Fallo de `cut_sample`: comportamiento actual (excepción capturada por el
  `except Exception` del reconocimiento, muestra sin match).

## Tests

`unittest`, junto a los existentes: no hay lógica pura nueva que testear
—el cambio es de orquestación—, así que la verificación es:

- Que la suite actual (30 tests) siga en verde.
- Una comprobación con dobles que confirme que `cut_sample` recibe offsets
  absolutos y que solo se descarga una vez, sin tocar la red.
- Medición en vivo comparando contra la referencia conocida
  (~30-40 min de descargas; objetivo: ejecución completa en menos de
  10 minutos).

## Fuera de alcance

- Cachear el audio descargado entre ejecuciones.
- Descargar solo los tramos con huecos usando un rango contiguo.
