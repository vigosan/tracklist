# Reconocimiento en paralelo con concurrencia adaptativa

**Fecha:** 2026-08-12
**Estado:** aprobado por Vicent

## Problema

Las muestras se reconocen de una en una con `await asyncio.sleep(1)` entre
ellas. Una ejecución típica del vídeo `_6r0Kd41jfs` tarda ~10 minutos para
~30 muestras. Medido en pruebas: 3 peticiones concurrentes a Shazam
tardaron 1,8 s en total y las tres acertaron, frente a ~6 s secuenciales.

El riesgo conocido es el rate limit (HTTP 429 → `FailedDecodeJson`), que ya
aparece hoy incluso a 1 petición/segundo.

## Objetivo

Reconocer las muestras de cada hueco en paralelo, ajustando la concurrencia
sola según cómo responda Shazam, sin cambiar el orden de los resultados ni
el contrato de eventos.

## Alcance

Solo las muestras dentro de un hueco. Los huecos siguen procesándose en
orden, uno tras otro (descarga → muestras en paralelo → siguiente hueco).
Descartado: descarga anticipada del siguiente hueco y huecos completos en
paralelo, porque rompen el modelo de "hueco activo" del TUI.

## Diseño

### Concurrencia adaptativa

Un objeto `Limiter` gobierna cuántas muestras se reconocen a la vez:

- Arranca en `START_WORKERS = 3`, con mínimo 1 y máximo 3.
- Ante un rate limit: `workers = max(1, workers // 2)` y se reinicia el
  contador de aciertos.
- Tras `RECOVER_AFTER = 6` muestras consecutivas sin rate limit:
  `workers = min(3, workers + 1)` y se reinicia el contador.

La concurrencia efectiva se aplica con un `asyncio.Semaphore` cuyo valor se
ajusta adquiriendo/liberando permisos, no recreándolo.

### Bucle de muestras

Por cada hueco, tras la descarga:

1. Se planifican los offsets con `plan_samples` (sin cambios).
2. Se cortan las muestras con ffmpeg (`asyncio.to_thread`), como hoy.
3. Cada muestra se reconoce dentro del semáforo. La pausa fija de 1 s entre
   muestras desaparece: el semáforo es el que regula el ritmo.
4. Los resultados se recogen con `asyncio.gather` y se ordenan por offset
   antes de emitir eventos, de modo que `merge_samples` sigue recibiendo
   las muestras en orden ascendente (requisito de `gap_residuals`).

### Eventos y pausa

El contrato de eventos no cambia: `("gap", ...)`, `("sample", offset,
match)`, `("backoff",)`, `("done", ...)`. Diferencias:

- Los eventos `("sample", ...)` se emiten **según van llegando**, no en
  orden de offset. El TUI ya los tolera: `merge_samples` se llama con la
  lista acumulada, que se mantiene ordenada por offset porque se inserta
  ordenado (`bisect.insort` por offset).
- `("backoff",)` se emite la primera vez que una muestra del hueco se
  topa con el rate limit, como hoy.
- La pausa (`resume`) se espera antes de adquirir el semáforo para cada
  muestra y antes de la descarga de cada hueco, como ahora.

### Reintento

Se conserva el comportamiento actual: ante `FailedDecodeJson`, esperar 30 s
y reintentar una vez; si vuelve a fallar, la muestra queda sin match. La
diferencia es que ahora, además, se reduce la concurrencia.

## Errores

- Rate limit: reduce concurrencia, emite `("backoff",)`, reintenta una vez
  tras 30 s.
- Excepción inesperada en una muestra: esa muestra queda sin match y el
  resto del hueco continúa (no aborta la ejecución).

## Tests

`unittest`, junto a los existentes:

- `Limiter`: valor inicial; reducción a la mitad ante rate limit (3→1 en
  dos avisos); recuperación tras `RECOVER_AFTER` aciertos; que nunca baje
  de 1 ni suba de 3.
- Orden de muestras: que una lista de resultados llegados en desorden
  produzca, tras la inserción ordenada, la misma secuencia que el flujo
  secuencial de hoy.

La ganancia real de tiempo y el comportamiento frente a Shazam se verifican
en vivo con `_6r0Kd41jfs`, comparando duración total y número de backoffs
con la ejecución secuencial conocida (~10 min).

## Fuera de alcance

- Descarga anticipada de huecos.
- Huecos en paralelo.
- Flag para fijar la concurrencia a mano.
