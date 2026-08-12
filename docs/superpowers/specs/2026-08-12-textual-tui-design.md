# TUI elaborado con Textual para el reconocimiento

**Fecha:** 2026-08-12
**Estado:** aprobado por Vicent

## Problema

El TUI actual (rich `Live`) es una lista plana que solo se repinta cuando
cae una muestra: entre muestras pasan 10-60 s sin que nada se mueva, y no
hay paneles, ni progreso, ni atajos de teclado.

## Objetivo

Un TUI de verdad (estilo Bagels): paneles con bordes titulados, tracklist
navegable, panel de estado con progreso y cronómetro, log de eventos, barra
de atajos, y repintado continuo para que se sienta vivo.

## Diseño

### Arquitectura

Fichero nuevo `recognize_tui.py` con una app de Textual. El reconocimiento
es `asyncio` y Textual corre su propio bucle, así que la app arranca
`recognize_gaps` como worker de Textual y recibe los eventos por el mismo
callback `progress` que ya existe. `recognize_gaps` y el formato de eventos
NO cambian.

El callback se invoca desde el worker; el estado se muta y el repintado lo
hace el temporizador (no cada evento), evitando repintados desde hilos
ajenos al bucle de Textual.

### Pantalla

```
╭─ Tracklist ──────────────╮╭─ Estado ────────╮
│  04:02 Earth n Days      ││ Hueco  3/7      │
│  05:34 Way Out West (?)  ││ ██████░░░░ 62%  │
│▸ 20:56 buscando ⣾        ││ Muestra 7/12    │
│  30:46 ?????             ││ Sugerencias 8   │
╰──────────────────────────╯│ Tiempo 04:31    │
                            ╰─────────────────╯
                            ╭─ Log ───────────╮
                            │ [+] Kate Project│
                            ╰─────────────────╯
 c Copiar   q Salir   ↑↓ Navegar
```

- **Tracklist** (izquierda, scrollable): las filas que ya produce
  `recognize_ui.tui_rows`. La fila activa se marca con `▸` y resalte.
  Auto-scroll a la fila activa hasta que el usuario pulse una flecha;
  a partir de ahí, scroll manual.
- **Estado**: hueco N/M, barra de progreso, muestra i/n, número de
  sugerencias acumuladas, cronómetro desde el inicio.
- **Log**: las líneas de `recognize_ui.pentest_line`, reutilizada tal cual.
- **Footer**: `c` copiar, `q` salir, `↑↓` navegar.

### Sensación de vivo

Un `set_interval(0.1)` de Textual repinta 10 veces/s:

- Spinner braille girando en la fila activa y en el panel de estado.
- Cronómetro corriendo.
- Barra de progreso interpolada dentro de la muestra en curso:
  `(muestras_completadas + fraccion) / muestras_totales` del hueco, donde
  `fraccion = min(0.95, (now - inicio_muestra) / ESTIMACION_MUESTRA)` con
  `ESTIMACION_MUESTRA = 4.0` segundos. Nunca retrocede ni llega a 1.0 hasta
  que la muestra se confirma.
- Con backoff activo, la fracción se congela y el panel muestra
  `rate limit - backoff`.

### Estado y función pura

El estado vive en un dict:

```python
{
  "gap": (indice, total) | None,
  "gap_range": (start, end) | None,
  "sample": int,            # muestras confirmadas del hueco actual
  "total": int,             # muestras planificadas del hueco actual
  "sample_started": float,  # monotonic del inicio de la muestra en curso
  "backoff": bool,
  "suggestions": int,
  "began": float,           # monotonic del arranque
}
```

`status_fields(state, now) -> dict` es pura y testeable, y devuelve:
`{"gap": "3/7", "percent": int, "sample": "7/12", "suggestions": int,
"elapsed": "04:31", "backoff": bool}`.

### Degradación y compatibilidad

- Sin TTY: modo pentest actual, sin cambios.
- Con TTY pero sin `textual` importable: aviso por stderr y modo pentest
  (no aborta). Mismo criterio que shazamio.
- `recognize_ui.py` conserva `pentest_line`, `tui_rows`, `entry_line`,
  `gap_line`, `gap_residuals` y `offer_clipboard`. Se elimina de él el
  presentador basado en rich `Live`, sustituido por el de Textual.
- `textual` se instala en el venv: `.venv/bin/pip install textual`.

### Copiado y salida

- `c` copia el tracklist renderizado hasta ese momento con `pbcopy`, sin
  salir; el log muestra `[*] copiado al portapapeles`.
- `q` cancela el reconocimiento y sale; se imprime lo obtenido hasta ese
  punto.
- Al terminar el reconocimiento la app se cierra sola y `main()` imprime el
  tracklist final por stdout como hasta ahora, con su oferta de
  portapapeles (`offer_clipboard`) intacta.

## Errores

- `textual` ausente: aviso y degradación a pentest.
- Excepción dentro del worker: la app se cierra y el error se propaga como
  hoy (sin capturarlo de forma especial).
- `pbcopy` fallido: línea de aviso en el Log, sin abortar.

## Tests

`unittest`, junto a los existentes:

- `status_fields`: porcentaje interpolado a mitad de muestra, congelado con
  backoff, tope de 0.95 antes de confirmar, formato de `elapsed`, y el caso
  inicial sin hueco activo.

El pintado de Textual, los atajos y el auto-scroll se verifican a mano con
el vídeo `_6r0Kd41jfs`.

## Fuera de alcance

- Editar o corregir sugerencias desde el TUI.
- Persistir el layout o temas configurables.
- Reproducir audio de las muestras.
