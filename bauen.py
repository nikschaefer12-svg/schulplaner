"""Baut templates/index.html aus oberflaeche.html.

Die Oberflaeche ist eine einzige Datei, die an zwei Orten laeuft: als Artifact
bei Claude und hier auf dem eigenen Server. Damit es nicht zwei Kopien gibt,
die auseinanderlaufen, steht sie einmal in oberflaeche.html und bekommt hier
nur den Rahmen und das Kennzeichen SCHULPLANER_WEB.

Aufruf:  python bauen.py
"""
import os
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
QUELLE = os.path.join(HIER, "oberflaeche.html")
ZIEL = os.path.join(HIER, "templates", "index.html")

KOPF = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Schulplaner: Hausaufgaben, Kalender, Stundenplan, Karteikarten, Noten und Zielnoten.">
<meta name="theme-color" content="#1F3BA8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0F1218" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Schulplaner">
<meta name="referrer" content="same-origin">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="icon" href="/static/icon-512.png" type="image/png">
<style>
  :root{color-scheme:light dark}
  html,body{margin:0}
  img{max-width:100%}
  [hidden]{display:none !important}
</style>
"""

# Das Kennzeichen muss vor dem Skript der Oberflaeche stehen, denn dort wird es
# beim ersten Durchlauf gelesen.
# Die Texterkennung wird erst beim ersten Scan nachgeladen, hier steht nur die
# Adresse. Feste Versionsnummer mit Absicht: eine offene Angabe wuerde eines
# Tages eine neue Fassung ziehen, die anders aufgerufen wird, und der Scan
# waere kaputt, ohne dass sich hier etwas geaendert haette.
OCR_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@6.0.1/dist/tesseract.min.js"

SCHALTER = ('<script>window.SCHULPLANER_WEB = true;'
            ' window.SCHULPLANER_OCR = "' + OCR_URL + '";</script>\n')


def bauen():
    if not os.path.exists(QUELLE):
        print("Fehlt: " + QUELLE)
        return 1
    roh = open(QUELLE, encoding="utf-8").read()
    if "</style>" not in roh:
        print("In oberflaeche.html fehlt der Stil-Block, so kann ich sie nicht teilen.")
        return 1
    kopfteil, rest = roh.split("</style>", 1)
    html = KOPF + kopfteil + "</style>\n</head>\n<body>\n" + SCHALTER + rest + "\n</body>\n</html>\n"
    os.makedirs(os.path.dirname(ZIEL), exist_ok=True)
    open(ZIEL, "w", encoding="utf-8").write(html)
    print("templates/index.html geschrieben, %d Zeichen" % len(html))
    return 0


if __name__ == "__main__":
    sys.exit(bauen())
