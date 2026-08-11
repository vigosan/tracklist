# TUI en vivo y logs para el reconocimiento

**Fecha:** 2026-08-11
**Estado:** aprobado por Vicent

## Problema

`--recognize` tarda ~9 minutos y solo emite una línea por hueco. No se ve
qué encuentra Shazam hasta el final, y el tracklist resultante hay que
copiarlo a mano.

## Objetivo

Durante `--recognize`, ver el tracklist completo en el terminal
actualizándose en vivo (huecos que pasan de `?????` a "buscando" a tema
encontrado), y al terminar poder copiarlo al portapapeles. En contextos no
interactivos (pipes), logs planos estilo pentest.

## Diseño

### Eventos de progreso

`recognize_gaps(video_id, gaps, progress)` gana un tercer parámetro
obligatorio: un callable que recibe tuplas:

- `("gap", indice, total, start, end)` — al empezar cada hueco (antes de
  descargar). `indice` desde 1.
- `("sample", offset, match)` — tras cada muestra; `match` es
  `{"artist", "title"}` o `None`.
- `("backoff",)` — al recibir el rate limit, antes de dormir 30 s.
- `("done", n_sugerencias, segundos)` — al terminar todo; `segundos` es el
  tiempo total medido con `time.monotonic()`, entero.

La línea actual "Reconociendo hueco…" desaparece de `recognize_gaps` (la
sustituyen los presentadores).

### Presentadores (`recognize_ui.py`, fichero nuevo)

Importado perezosamente solo en el camino `--recognize`, junto a shazamio.
Dos modos, elegidos en `main()` según `sys.stderr.isatty()`:

**TUI en vivo (TTY):** usa `rich` (`Live`). Pinta desde el arranque todas
las filas: entradas de Content ID (verde), huecos (`?????`, gris). El hueco
activo muestra spinner y `buscando (muestra i/n)…`; cada match aparece
inmediatamente bajo él en amarillo con ` (?)`. Al completarse un hueco sus
filas se consolidan: sugerencias fusionadas (via `merge_samples`) y tramos
restantes recalculados. `("backoff",)` muestra `backoff 30s` en la fila
activa. La construcción de filas es una función pura
`tui_rows(entries, terminado, activo) -> list[str]` (strings con markup de
rich) para poder testearla sin terminal.

**Logs pentest (sin TTY):** una línea por evento vía función pura
`pentest_line(event) -> str | None` (None = evento sin línea), impresa a
stderr por un envoltorio mínimo. Formato, sin colores ANSI:

```
[*] hueco 1/7 (00:00-04:02)
[+] 01:15 Duran Duran - Too Late Marlene
[-] 02:15 sin match
[!] rate limit - backoff 30s
[*] 8 sugerencias en 09:12
```

Timestamps con el `hhmmss` existente.

### Portapapeles

Al acabar el TUI: el tracklist final se imprime por stdout como siempre
(los pipes no cambian). Después, solo si `sys.stdin.isatty()` y
`sys.stderr.isatty()`, pregunta por stderr `¿Copiar al portapapeles? [S/n] `;
Enter o `s`/`S` copia el texto exacto de `render()` ejecutando `pbcopy`
(subprocess, stdin). En modo no interactivo no se pregunta nada.

### Dependencias

`rich` en el `.venv` (`.venv/bin/pip install rich`), junto a shazamio. El
flujo sin `--recognize` sigue siendo stdlib puro con el python del sistema.

## Errores

- `recognize_ui` o `rich` no importables con `--recognize`: mismo patrón
  que shazamio — salir con el comando de instalación exacto.
- Fallo de `pbcopy`: aviso por stderr, sin abortar (el tracklist ya está
  impreso).
- Los avisos existentes de descarga fallida siguen yendo por stderr y
  conviven con ambos modos.

## Tests

`unittest`, junto a los existentes:

- `pentest_line`: una aserción por tipo de evento, incluyendo `None` para
  eventos sin línea si los hubiera, y el formato exacto de gap/match/
  no-match/backoff/done.
- `tui_rows`: filas para un estado dado — entradas verdes, hueco activo con
  contador de muestra, sugerencias parciales bajo el hueco activo, huecos
  pendientes en gris.

El Live de rich, el prompt y `pbcopy` se verifican a mano con el vídeo
`_6r0Kd41jfs`.

## Fuera de alcance

- Captura de teclado durante la ejecución (tecla `c`).
- TUI para el flujo sin `--recognize`.
- Colores/temas configurables.
