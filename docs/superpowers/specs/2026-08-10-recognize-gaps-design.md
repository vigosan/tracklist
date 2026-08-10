# Reconocimiento de huecos con Shazam para tracklist

**Fecha:** 2026-08-10
**Estado:** aprobado por Vicent

## Problema

Content ID no reclama todos los tramos de un directo: en el último vídeo
quedaron 33:19 de 1:06:58 sin identificar, repartidos en 7 huecos `?????`.
Esos temas hay que identificarlos a mano.

## Objetivo

`tracklist.py --recognize <video>` rellena los huecos con sugerencias de
Shazam, marcadas como tales, sin tocar el flujo actual cuando el flag no se
usa.

## Enfoque elegido

Shazam vía la librería `shazamio` (API no oficial, gratuita, fuerte en
música dance/remixes). Descartados: ACRCloud y AudD (alta/coste),
AcoustID/MusicBrainz (solo casa grabaciones exactas; inútil con mezclas).

## Diseño

### Flujo con `--recognize`

1. Se generan entradas y huecos como hoy (`extract` + `find_gaps`).
2. Por cada hueco se descarga solo ese tramo de audio:
   `yt-dlp -x --download-sections "*{inicio}-{fin}" --cookies-from-browser
   brave` a un directorio temporal.
3. De cada tramo se cortan con ffmpeg muestras de 10 s: la primera en
   `inicio + 15 s` y luego cada 60 s, mientras la muestra completa quepa en
   el hueco. Un hueco de 90 s produce 2 muestras.
4. Cada muestra pasa por `shazamio` (async, `asyncio.run`, ~1 s de pausa
   entre llamadas). Muestras consecutivas del mismo hueco con el mismo
   (artista, tema) se fusionan en una sugerencia:
   - inicio = offset absoluto de la primera muestra de la racha
   - duración = (offset última muestra + 10 s) − offset primera muestra
   Muestras sin resultado rompen la racha.
5. Las sugerencias se combinan con las entradas de Content ID y se
   recalculan los huecos restantes con `find_gaps`. El render marca las
   sugerencias con ` (?)` al final de la línea; los tramos que Shazam no
   reconoció siguen saliendo como `?????`.
6. El resumen final pasa a:
   `{n} identificados, {s} sugeridos, {k} huecos ({t} sin identificar de {total})`
   (la parte `{s} sugeridos, ` solo aparece cuando hay sugerencias).

### Dependencias

- `shazamio` instalado en el `.venv` del proyecto. Import perezoso solo
  dentro del camino `--recognize`: sin el flag, el script sigue siendo
  stdlib puro y corre con el python del sistema.
- `ffmpeg` (ya instalado vía Homebrew) para cortar muestras; yt-dlp ya lo
  usa para las secciones.
- Riesgo a verificar en la implementación: compatibilidad de shazamio con
  Python 3.14; si no instala en el `.venv`, se decidirá con el usuario
  (bajar versión de python del venv u otra alternativa).

### Errores

- `--recognize` sin shazamio importable: salir con el comando exacto de
  instalación (`.venv/bin/pip install shazamio`).
- Fallo de descarga de un hueco: avisar por stderr y continuar con los
  demás huecos.
- Muestra sin resultado de Shazam: se ignora (silencio, mezcla, tema
  desconocido).
- Los timestamps de sugerencias son aproximados (granularidad de muestreo,
  ±1 min): por eso van marcadas con `(?)`.

## Tests

`unittest`, junto a los existentes:

- Planificación de muestras: offsets generados para un hueco dado,
  incluyendo bordes (hueco mínimo de 90 s → 2 muestras; muestra que no
  cabe → no se genera).
- Fusión de rachas: muestras consecutivas iguales → una sugerencia con
  inicio y duración correctos; racha rota por muestra sin resultado.
- Render: línea con marcador ` (?)`, resumen con `sugeridos` solo cuando
  procede, y `?????` para tramos aún sin cubrir.

La parte de red (yt-dlp + Shazam) se verifica manualmente con el vídeo
`_6r0Kd41jfs`.

## Fuera de alcance

- Persistencia/caché de reconocimientos entre ejecuciones.
- Afinar timestamps de sugerencia por debajo del intervalo de muestreo.
- Reconocimiento de tramos ya reclamados por Content ID.
