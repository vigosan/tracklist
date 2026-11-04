import os

MESSAGES = {
    "en": {
        "render_gap": "????? (unidentified, until {end})",
        "render_suggested": "{count} suggested, ",
        "render_summary": "{identified} identified, {suggested}{gaps} gaps ({missing} unidentified of {total})",
    },
    "es": {
        "render_gap": "????? (sin identificar, hasta {end})",
        "render_suggested": "{count} sugeridos, ",
        "render_summary": "{identified} identificados, {suggested}{gaps} huecos ({missing} sin identificar de {total})",
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
