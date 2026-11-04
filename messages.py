import os

MESSAGES = {
    "en": {
        "render_gap": "????? (unidentified, until {end})",
        "render_suggested": "{count} suggested, ",
        "render_summary": "{identified} identified, {suggested}{gaps} gaps ({missing} unidentified of {total})",
        "log_gap": "[*] gap {index}/{total} ({start}-{end})",
        "log_no_match": "[-] {offset} no match",
        "log_no_claims": "[*] no claims: scanning the whole video with Shazam",
        "log_claim": "[*] {count} Content ID claim",
        "log_claims": "[*] {count} Content ID claims",
        "log_download_start": "[*] downloading audio...",
        "log_download_cached": "[*] audio already downloaded",
        "log_download_done": "[*] audio downloaded",
        "log_cached": "[*] {done}/{total} samples already analysed, {left} to go",
        "log_resolved": "[*] {offset} recovered after the backoff",
        "log_workers_down": "[!] dropping to 1 concurrent request",
        "log_workers_up": "[*] raising to {count} concurrent requests",
        "log_backoff": "[!] rate limit - waiting {seconds}s (attempt {attempt}/{retries})",
        "log_done": "[*] {count} suggestions in {elapsed}",
        "row_downloading": "downloading",
        "row_searching": "searching (sample {sample}/{total})",
        "tui_missing": "Warning: TUI disabled (textual is missing).",
        "yes": "y",
        "ask_clipboard": "Copy to clipboard? [Y/n] ",
        "copied": "Copied.",
        "copy_failed": "Warning: could not copy to the clipboard",
    },
    "es": {
        "render_gap": "????? (sin identificar, hasta {end})",
        "render_suggested": "{count} sugeridos, ",
        "render_summary": "{identified} identificados, {suggested}{gaps} huecos ({missing} sin identificar de {total})",
        "log_gap": "[*] hueco {index}/{total} ({start}-{end})",
        "log_no_match": "[-] {offset} sin match",
        "log_no_claims": "[*] sin reclamaciones: se analiza el video entero con Shazam",
        "log_claim": "[*] {count} reclamacion de Content ID",
        "log_claims": "[*] {count} reclamaciones de Content ID",
        "log_download_start": "[*] descargando audio...",
        "log_download_cached": "[*] audio ya descargado",
        "log_download_done": "[*] audio descargado",
        "log_cached": "[*] {done}/{total} muestras ya analizadas, faltan {left}",
        "log_resolved": "[*] {offset} recuperado tras el backoff",
        "log_workers_down": "[!] bajando a 1 peticion simultanea",
        "log_workers_up": "[*] subiendo a {count} peticiones simultaneas",
        "log_backoff": "[!] rate limit - esperando {seconds}s (intento {attempt}/{retries})",
        "log_done": "[*] {count} sugerencias en {elapsed}",
        "row_downloading": "descargando",
        "row_searching": "buscando (muestra {sample}/{total})",
        "tui_missing": "Aviso: TUI desactivado (falta textual).",
        "yes": "s",
        "ask_clipboard": "¿Copiar al portapapeles? [S/n] ",
        "copied": "Copiado.",
        "copy_failed": "Aviso: no se pudo copiar al portapapeles",
    },
}


def detect(environ):
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if environ.get(name):
            return "es" if environ[name].startswith("es") else "en"
    return "en"


LANGUAGE = detect(os.environ)


def t(key, **values):
    return MESSAGES[LANGUAGE][key].format(**values)
