# Arranque instantáneo y controles del TUI

**Fecha:** 2026-08-12
**Estado:** aprobado por Vicent

## Problema

1. Pasan ~4 s de pantalla en blanco antes de que aparezca el TUI. Medido:
   1,3 s leyendo cookies de Brave, 2,3 s pidiendo reclamaciones a Studio,
   0,4 s pidiendo datos del vídeo. Parece que no funciona.
2. Durante la descarga de cada hueco (10-30 s) el TUI se congela: yt-dlp y
   ffmpeg bloquean el mismo bucle que repinta la pantalla.
3. No se puede pausar, cambiar de vídeo ni descartar lo encontrado sin
   matar el proceso.

## Objetivo

El TUI aparece al instante y nunca se queda congelado, y desde él se puede
pausar (`p`), empezar con otro vídeo (`n`) y vaciar lo encontrado (`x`).

## Diseño

### Arranque instantáneo

Con terminal, `main()` deja de hacer red antes del TUI: valida argumentos,
resuelve el id y arranca la app inmediatamente. Las tres fases de carga se
ejecutan dentro del worker de la app, anunciándose en el panel de estado
mediante `phase_label(phase)`:

| phase | texto |
|-------|-------|
| `cookies` | `Leyendo cookies de Brave...` |
| `claims` | `Pidiendo reclamaciones a Studio...` |
| `video` | `Pidiendo datos del video...` |
| `recognizing` | (estado normal de reconocimiento) |

Errores durante la carga (sesión caducada, sin reclamaciones con
timestamp): la app se cierra y `main()` sale con el mismo mensaje de
siempre por stderr, sin traza. Se implementa dejando que la app guarde el
error en un atributo y `main()` lo consulte tras `run_app`.

Sin terminal, el flujo actual no cambia: la carga ocurre antes, como hoy.

### Descargas sin congelar

En `recognize_gaps`, las dos llamadas bloqueantes pasan a hilo:

- `source = await asyncio.to_thread(download_gap, video_id, start, end, directory)`
- `sample = await asyncio.to_thread(cut_sample, source, offset - start, directory)`

Es un cambio de comportamiento nulo para el camino síncrono (`asyncio.run`
sigue esperando el resultado) y libera el bucle de Textual mientras yt-dlp
y ffmpeg trabajan.

### Controles

Se suman a las existentes (`c` copiar, `q` salir):

- **`p`**: pausa/reanuda. Un `asyncio.Event` (`resume`) que el bucle de
  muestras espera (`await resume.wait()`) antes de cada muestra. En pausa
  el panel muestra `PAUSA` y el cronómetro sigue corriendo; no se consume
  ni descarga nada nuevo. La pausa surte efecto tras la muestra en curso.
- **`n`**: nuevo vídeo. Muestra un `Input` en el panel de estado; acepta
  URL o ID (mismo `parse_video_id`). Al confirmar, descarta el trabajo
  actual y reinicia con el nuevo vídeo desde la fase `cookies` (la sesión
  ya obtenida se reutiliza; se repiten `claims` y `video`).
- **`x`**: vaciar. Descarta las sugerencias del vídeo actual y reinicia su
  reconocimiento desde el primer hueco.

`n` y `x` descartan trabajo: si hay sugerencias acumuladas, piden
confirmación en el panel (`¿Descartar N sugerencias? s/n`); cualquier tecla
distinta de `s` cancela. Sin sugerencias, actúan directamente.

Reiniciar (por `n` o `x`) cancela el worker en curso, limpia
`suggestions`, `rows_state`, el log y los contadores, y lanza un worker
nuevo.

### Footer

`p Pausa   n Nuevo   x Vaciar   c Copiar   q Salir`

## Errores

- Fallo de red/sesión durante la carga: mensaje por stderr tras cerrarse el
  TUI, con el texto actual de cada caso.
- Vídeo nuevo sin reclamaciones con timestamp: se informa en el log y se
  mantiene el vídeo anterior en pantalla (no cierra la app).
- `pbcopy` fallido: línea en el log, sin abortar (como hoy).

## Tests

`unittest`, junto a los existentes:

- `phase_label`: las cuatro fases y una desconocida.
- Transiciones de estado del reinicio: una función pura
  `initial_state(now)` usada tanto al arrancar como al reiniciar, con test
  de que deja contadores a cero y sin hueco activo.

El pintado, las teclas, la entrada de texto y las confirmaciones se
verifican a mano con el vídeo `_6r0Kd41jfs`.

## Fuera de alcance

- Caché en disco de reclamaciones.
- Reanudar un reconocimiento cancelado desde donde iba.
- Editar sugerencias desde el TUI.
