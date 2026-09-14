# Schulplaner — Server und Website

Ein Flask-Dienst mit der kompletten Schulplaner-Oberfläche. Hausaufgaben,
Kalender, Stundenplan, Karteikarten mit Leitner-Boxen, Noten, Zielnoten und
die Aufwands-Empfehlung. Aufgebaut wie das Kill-ELO-Backend: SQLite lokal,
Postgres in der Cloud, `gunicorn` über das `Procfile`.

Anders als bei Kill-ELO ist hier nichts öffentlich. Auf der Seite stehen
Hausaufgaben und Noten, deshalb braucht jeder Zugriff eine Anmeldung.

## Lokal starten

```bash
pip install -r requirements.txt
python app.py
```

Dann <http://localhost:8000/> öffnen. `/health` antwortet `ok`, auch wenn die
Datenbank gerade klemmt. `/diag` sagt, woran es liegt.

```bash
python test_app.py
```

läuft 44 Prüfungen gegen eine Wegwerf-Datenbank. Kein Server nötig.

## Aufbau

| Datei | Wofür |
| --- | --- |
| `app.py` | Server, Anmeldung, API |
| `oberflaeche.html` | die ganze Seite, eine Datei, hier wird sie bearbeitet |
| `bauen.py` | macht daraus `templates/index.html` |
| `templates/index.html` | das Ergebnis, wird ausgeliefert und mit eingecheckt |
| `static/` | Symbole und das Manifest für den Home-Bildschirm |
| `test_app.py` | Prüfungen ohne laufenden Server |

Nach jeder Änderung an `oberflaeche.html`:

```bash
python bauen.py
```

`oberflaeche.html` ist dieselbe Datei, die auch als Claude-Artifact läuft.
Die Vorlage setzt beim Bauen `window.SCHULPLANER_WEB = true`, und daran
erkennt die Seite, dass sie ihre Daten über `/api` holen soll statt aus dem
Claude-Speicher. So gibt es nur eine Oberfläche und nicht zwei Kopien, die
auseinanderlaufen.

Die Seite wird bewusst **nicht** durch Jinja geschickt, sondern direkt als
Datei ausgeliefert. Im CSS steht `@media (min-width:900px){#toast{...}}`, und
`{#` ist für Jinja der Anfang eines Kommentars. Dynamisch ist an der Seite
ohnehin nichts.

## Endpunkte

| Methode | Pfad | Zweck |
| --- | --- | --- |
| GET | `/` | die Seite |
| POST | `/api/konto` | Konto anlegen, `{name, passwort}` |
| POST | `/api/anmelden` | anmelden, setzt das Sitzungs-Cookie |
| POST | `/api/abmelden` | abmelden |
| POST | `/api/passwort` | Passwort ändern, `{alt, neu}` |
| GET | `/api/ich` | wer angemeldet ist |
| GET | `/api/daten` | alles zum angemeldeten Konto |
| PUT | `/api/profil` | Klassenstufe, Schulform, Bundesland, Farbe |
| PUT | `/api/cfg` `/api/faecher` `/api/plan` | die drei Einzeldokumente |
| PUT/DELETE | `/api/hausaufgabe/<id>` | eine Hausaufgabe |
| PUT/DELETE | `/api/test/<id>` | ein Test mit Karten und Noten |
| DELETE | `/api/alles` | alle Daten des Kontos, Konto bleibt |
| GET | `/health` `/diag` | Zustand |

## Wie die Daten liegen

Zwei Tabellen. `konten` hält Name, Passwort-Prüfsumme und die Angaben zur
Schule. `eintraege` hält alles andere als JSON, eine Zeile je Gegenstand, mit
`(konto_id, art, schluessel)` als Schlüssel.

Das JSON ist genau das, was die Oberfläche ohnehin benutzt. Dadurch muss das
Schema nicht jedes Mal mitwachsen, wenn in der App ein Feld dazukommt. Der
Preis: man kann nicht nach einzelnen Feldern suchen. Für einen Planer mit ein
paar hundert Einträgen pro Person ist das der bessere Handel.

## Passwörter

Gespeichert wird nie das Passwort, sondern PBKDF2-HMAC-SHA256 mit 210000
Runden und 16 Byte Salz je Konto. Verglichen wird mit `hmac.compare_digest`,
damit die Antwortzeit nichts verrät. Falscher Name und falsches Passwort geben
dieselbe Meldung, sonst könnte man herausfinden, welche Namen es gibt. Nach
zehn Fehlversuchen ist ein Name 15 Minuten gesperrt.

Es gibt **keine** Zurücksetzung per Mail, weil es keine Mailadressen gibt.
Passwort vergessen heißt: Konto neu anlegen.

## Ins Netz stellen

**1 — Datenbank (Neon, kostenlos).** Auf <https://neon.com> anmelden, ein
Projekt anlegen, die Verbindungszeichenfolge kopieren. Sie fängt mit
`postgresql://` an.

**2 — Web-Dienst (Render, kostenlos).** Diesen Ordner in ein GitHub-Repository
legen, dann auf <https://render.com> **New → Web Service**, das Repository
verbinden und einstellen:

- Build Command: `pip install -r requirements.txt`
- Start Command: kommt aus dem `Procfile`, muss man nicht eintragen
- Environment Variables:
  - `DATABASE_URL` = die Zeichenfolge von Neon
  - `SCHULPLANER_SECRET` = eine lange zufällige Zeichenkette

Den Schlüssel kannst du dir so erzeugen:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`secrets.token_hex(32)` gibt 64 Hex-Zeichen aus einer kryptografisch
geeigneten Quelle. Setzt du ihn nicht, erzeugt der Dienst bei jedem Start
einen neuen, und alle sind nach jedem Neustart abgemeldet.

Deployen, dann die Adresse öffnen, die Render vergibt. Die Tabellen entstehen
bei der ersten Anfrage von selbst.

**Kostenloser Plan:** Render fährt den Dienst nach etwa 15 Minuten ohne
Zugriff herunter. Der erste Aufruf danach dauert dann eine halbe Minute. Neon
macht dasselbe mit der Datenbank. Beides ist normal und kein Fehler.

## Auf dem iPad

Seite in Safari öffnen, anmelden, Teilen-Knopf, **Zum Home-Bildschirm**.
Danach startet sie ohne Adressleiste und mit eigenem Symbol.

## Was hier nicht geht

Lernziele aus einem Foto auslesen, Karteikarten und Quizfragen automatisch
erzeugen, Erklärungen und Spickzettel. Das läuft über Claude und nur in der
Artifact-Fassung. Ein API-Schlüssel dafür gehört nicht in eine Seite, die
öffentlich erreichbar ist, denn er wäre von jedem auslesbar.

Die Oberfläche merkt das von selbst und blendet die betroffenen Knöpfe aus,
statt Fehler zu zeigen. Tests und Themen trägt man hier von Hand ein, alles
Weitere rechnet der Planer wie gewohnt.
