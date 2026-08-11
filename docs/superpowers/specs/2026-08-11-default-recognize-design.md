# Reconocimiento por defecto y prompt de vídeo

**Fecha:** 2026-08-11
**Estado:** aprobado por Vicent

## Problema

El reconocimiento de huecos exige acordarse del flag `--recognize`, y sin
argumento de vídeo el script suelta un error de argparse. Además las URLs
de Studio (`studio.youtube.com/video/<id>/edit`) no se parsean.

## Objetivo

`tracklist` reconoce huecos por defecto (degradando con aviso si faltan
dependencias), pide el vídeo interactivamente cuando falta el argumento y
hay terminal, y acepta URLs de Studio.

## Diseño

### `--recognize` por defecto

- El flag pasa a `argparse.BooleanOptionalAction` con `default=True`:
  `--recognize` sigue aceptándose (redundante) y aparece `--no-recognize`.
- En `main()`, si toca reconocer (`args.recognize` y hay huecos), primero
  se comprueba la disponibilidad con `import shazamio` en un try. Si falla:
  aviso por stderr y el tracklist sale sin sugerencias, sin abortar:

  ```
  Aviso: reconocimiento de huecos desactivado (faltan dependencias).
  Para activarlo: .venv/bin/python3 tracklist.py <video>
  (una vez: .venv/bin/pip install audioop-lts shazamio==0.4.0.1 rich)
  ```

- Con dependencias disponibles, el flujo actual (presenter + recognize_gaps
  + offer_clipboard) corre igual que hoy con el flag.
- El mensaje de error dentro de `recognize_gaps` para shazamio ausente
  queda como red de seguridad y no cambia.

### Prompt de vídeo

- Sin `args.video` y con `sys.stdin.isatty()` y `sys.stderr.isatty()`:
  se pregunta `URL o ID del video: ` por stderr y se lee con `input()`.
  Respuesta vacía o EOF → `parser.error("indica la URL o el ID del video")`
  (el mismo error de hoy).
- Sin terminal: `parser.error` directo, como hoy.

### `parse_video_id`

La extracción del ID sale de `main()` a una función pura:

```python
def parse_video_id(text):
    match = re.search(r"(?:v=|youtu\.be/|video/)([\w-]{11})", text)
    return match.group(1) if match else text
```

Acepta URL de watch (`v=`), corta (`youtu.be/`), de Studio (`video/`) y el
ID pelado (fallback como hoy).

## Errores

- Dependencias ausentes: aviso único por stderr (texto de arriba), nunca
  aborta.
- Prompt vacío/EOF: mismo `parser.error` actual.

## Tests

`unittest`, junto a los existentes:

- `parse_video_id`: URL de watch, youtu.be, URL de Studio (formato
  `https://studio.youtube.com/video/<id>/edit`) e ID pelado.

El prompt interactivo y la degradación sin dependencias se verifican
manualmente (TTY real / python del sistema).

## Fuera de alcance

- Selector de vídeos del canal.
- Oferta de portapapeles en ejecuciones sin reconocimiento.
