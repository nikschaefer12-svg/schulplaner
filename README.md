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

läuft 60 Prüfungen gegen eine Wegwerf-Datenbank. Kein Server nötig, und die
KI läuft dabei im Testmodus, es entstehen also keine Kosten.

## Aufbau

| Datei | Wofür |
| --- | --- |
| `app.py` | Server, Anmeldung, API |
| `oberflaeche.html` | die ganze Seite, eine Datei, hier wird sie bearbeitet |
| `bauen.py` | macht daraus `templates/index.html` |
| `templates/index.html` | das Ergebnis, wird ausgeliefert und mit eingecheckt |
| `static/` | Symbole und das Manifest für den Home-Bildschirm |
| `ki.py` | die Claude-Aufrufe, hier liegt der Schlüssel |
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
| GET | `/api/ki` | ob Claude verfügbar ist, Grenzen, heutiger Verbrauch |
| POST | `/api/ki/text` | Prompt rein, Text raus |
| POST | `/api/ki/json` | Prompt rein, geparstes JSON raus |
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

### Neue Fassung einspielen

Kommt Render ueber das Feld fuer oeffentliche Git-Adressen an das Repo, gibt es
keine Verbindung zu GitHub und damit auch keine Benachrichtigung bei neuen
Commits. Render merkt von selbst also nichts. Nach jedem `git push`:

Render-Dashboard, der Dienst, oben rechts **Manual Deploy**, dann
**Deploy latest commit**.

Ob der neue Stand wirklich draussen ist, sagt `/diag`. Dort stehen der Commit
und unter `seite` die Groesse, der Zeitstempel und ob die Texterkennung drin
ist. Sieht die Seite unveraendert aus, ist das die erste Stelle zum Nachsehen.

Wer das nicht jedes Mal von Hand machen will, verbindet in Render unter
**Credentials** das GitHub-Konto, dem das Repo gehoert. Dann laeuft Auto-Deploy.

**Kostenloser Plan:** Render fährt den Dienst nach etwa 15 Minuten ohne
Zugriff herunter. Der erste Aufruf danach dauert dann eine halbe Minute. Neon
macht dasselbe mit der Datenbank. Beides ist normal und kein Fehler.

## Auf dem iPad

Seite in Safari öffnen, anmelden, Teilen-Knopf, **Zum Home-Bildschirm**.
Danach startet sie ohne Adressleiste und mit eigenem Symbol.

## Claude auf dem Server

Setzt du `ANTHROPIC_API_KEY`, kann die Seite alles, was die Artifact-Fassung
auch kann: Lernziele aus Fotos lesen, Karteikarten und Quizfragen erzeugen,
Erklärungen, Lernpläne und Spickzettel schreiben.

Der Schlüssel liegt dabei **nur** in den Umgebungsvariablen des Servers. Das
Gerät schickt seine Anfrage an `/api/ki/text` oder `/api/ki/json`, der Server
ruft Claude und gibt nur die fertige Antwort zurück. Der Schlüssel wird nie
mit ausgeliefert.

### Umgebungsvariablen

| Name | Vorgabe | Wofür |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | leer | ohne ihn ist die KI aus |
| `KI_MODELL` | `claude-opus-5` | `claude-sonnet-5` oder `claude-haiku-4-5` sind billiger |
| `KI_LIMIT_KONTO` | `80` | Anfragen je Konto und Tag |
| `KI_LIMIT_GESAMT` | `300` | Anfragen über alle Konten und Tag |
| `SCHULPLANER_REG_CODE` | leer | ist er gesetzt, braucht man ihn zum Anlegen eines Kontos |
| `KI_TESTMODUS` | leer | `1` liefert feste Antworten ohne echten Aufruf, zum Prüfen |

Den Schlüssel gibt es unter <https://console.anthropic.com/settings/keys>.

### Warum der Einladungscode wichtig ist

Die Seite ist öffentlich erreichbar, jeder kann ein Konto anlegen. Ohne
`SCHULPLANER_REG_CODE` könnte also jeder Fremde die KI auf deine Rechnung
benutzen. Setz den Code, sobald ein Schlüssel hinterlegt ist.

Die beiden Tageslimits sind die zweite Sicherung. Gezählt wird **vor** dem
Aufruf, ein abgebrochener Aufruf zählt also mit. Lieber einmal zu viel gezählt
als ein Aufruf, der Geld kostet und nirgends auftaucht.

### Was die KI kostet

Abgerechnet wird nach Ein- und Ausgabe. Ein Scan mit Foto und ein Satz
Karteikarten liegen im Bereich weniger Cent, eine Erklärung darunter. Die
aktuellen Preise stehen unter <https://anthropic.com/pricing>.

Der Aufwand ist pro Aufgabe eingestellt: kurze Texte laufen auf `low`,
Auswertungen von Fotos und das Erzeugen von Karten auf `medium`. Das hält
Antwortzeit und Kosten unten.

### Ohne Schlüssel: Texterkennung auf dem Gerät

Ist kein Schlüssel gesetzt, fällt die Seite auf `tesseract.js` zurück. Sie
lädt es beim ersten Scan nach und erkennt den Text im Browser selbst. Das Bild
verlässt das Gerät nicht, und es kostet nichts.

Beim ersten Mal kommen etwa 10 MB an Sprachmodell dazu (`deu.traineddata` und
der wasm-Kern), danach liegen sie im Browser-Cache. Ein Scan dauert dann ein
bis zwei Sekunden.

`textAuswerten` in `oberflaeche.html` zerlegt den erkannten Text nach Regeln:
Fach über Stichwörter, Datum über ein Muster wie `25.09.`, jede übrige Zeile
wird ein Thema. Überschriften wie `Lernziele:` fallen raus. Kommt nichts
Brauchbares heraus, landet der Rohtext im Textfeld statt in einer
Fehlermeldung.

Grenzen: gedruckter Text wird gut gelesen, Handschrift und Tafelbilder
schlecht. Die Themen werden getrennt, aber nicht verstanden. Karteikarten und
Quiz gibt es auf diesem Weg nicht.

### Grenze des Relais

`/api/ki/text` und `/api/ki/json` nehmen den Prompt entgegen, den die Seite
gebaut hat, und reichen ihn weiter. Wer ein Konto hat, kann darüber also
beliebige Anfragen an Claude stellen, nicht nur die aus der Oberfläche. Der
Einladungscode und die Tageslimits sind genau dafür da. Wer das enger haben
will, baut die Prompts serverseitig in `ki.py` und nimmt vom Gerät nur noch
die Daten entgegen.
