"""Durchlauf gegen eine frische Datenbank - ohne laufenden Server.

Aufruf:  python test_app.py

Nutzt Flasks Test-Client, startet also nichts im Netz. Die Datenbank ist eine
Wegwerf-Datei, die am Anfang geloescht wird.

Achtung: Geprueft wird der SQLite-Weg. Der Postgres-Weg (DATABASE_URL gesetzt)
benutzt teils anderes SQL, siehe USE_PG in app.py, und laesst sich nur gegen
eine echte Postgres-Datenbank pruefen.
"""
import os
import sys
import tempfile

DB = os.path.join(tempfile.gettempdir(), "schulplaner_test.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["SCHULPLANER_DB"] = DB
os.environ["SCHULPLANER_SECRET"] = "test-geheimnis-nur-fuer-den-durchlauf"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

failed = []


def check(label, condition, extra=""):
    print(("  OK  " if condition else "FAIL  ") + label + ("  " + str(extra) if extra else ""))
    if not condition:
        failed.append(label)


def neuer_client():
    return app.app.test_client()


c = neuer_client()

# ---- Zustand ohne Anmeldung ---------------------------------------------
r = c.get("/health")
check("/health antwortet ok", r.status_code == 200 and r.data == b"ok", r.status_code)

r = c.get("/")
check("Startseite wird ausgeliefert", r.status_code == 200 and b"Schulplaner" in r.data, r.status_code)
check("Seite traegt das Web-Kennzeichen", b"SCHULPLANER_WEB" in r.data)

r = c.get("/api/ich")
check("Ohne Anmeldung: angemeldet=false", r.status_code == 200 and r.get_json()["angemeldet"] is False)

r = c.get("/api/daten")
check("Daten ohne Anmeldung sind gesperrt", r.status_code == 401, r.status_code)

r = c.put("/api/hausaufgabe/abc", json={"titel": "x"})
check("Schreiben ohne Anmeldung ist gesperrt", r.status_code == 401, r.status_code)

# ---- Konto anlegen -------------------------------------------------------
r = c.post("/api/konto", json={"name": "ab", "passwort": "geheim1234"})
check("Zu kurzer Name wird abgelehnt", r.status_code == 400, r.status_code)

r = c.post("/api/konto", json={"name": "nik", "passwort": "kurz"})
check("Zu kurzes Passwort wird abgelehnt", r.status_code == 400, r.status_code)

r = c.post("/api/konto", json={"name": "nik", "passwort": "geheim1234"})
check("Konto wird angelegt", r.status_code == 201, r.status_code)
check("Konto gilt als nicht eingerichtet", r.get_json()["konto"]["eingerichtet"] is False)

r = c.post("/api/konto", json={"name": "NIK", "passwort": "andersaber8"})
check("Gleicher Name in anderer Schreibweise wird abgelehnt", r.status_code == 409, r.status_code)

with app.app.app_context():
    d = app.connect()
    row = app.ex(d, "SELECT pw_hash, pw_salz FROM konten WHERE name = ?", ("nik",)).fetchone()
    d.close()
check("Passwort steht nicht im Klartext in der Datenbank", "geheim1234" not in row["pw_hash"])
check("Jedes Konto hat ein eigenes Salz", len(row["pw_salz"]) == app.SALZ_BYTES * 2, row["pw_salz"])

# ---- Profil und Daten ----------------------------------------------------
r = c.put("/api/profil", json={"klasse": "9", "schulform": "Gymnasium", "land": "Hessen", "farbe": 146})
check("Profil laesst sich speichern", r.status_code == 200 and r.get_json()["konto"]["klasse"] == "9")
check("Konto gilt jetzt als eingerichtet", r.get_json()["konto"]["eingerichtet"] is True)

r = c.put("/api/hausaufgabe/h1", json={"id": "h1", "fach": "Mathematik", "titel": "S. 42", "faellig": "2026-09-20", "erledigt": False})
check("Hausaufgabe wird gespeichert", r.status_code == 200, r.status_code)

r = c.put("/api/test/t1", json={"id": "t1", "fach": "Biologie", "titel": "Zellen", "datum": "2026-09-25", "themen": [], "karten": [], "versuche": []})
check("Test wird gespeichert", r.status_code == 200, r.status_code)

r = c.put("/api/faecher", json=[{"name": "Mathematik", "h": 214}])
check("Faecher werden gespeichert", r.status_code == 200, r.status_code)

r = c.put("/api/cfg", json={"erklaerstufe": "einfach", "ziele": {"Mathematik": 2}})
check("Einstellungen werden gespeichert", r.status_code == 200, r.status_code)

r = c.put("/api/unsinn", json={})
check("Unbekannter Bereich wird abgelehnt", r.status_code == 404, r.status_code)

r = c.put("/api/hausaufgabe/../../etc", json={"a": 1})
check("Schluessel mit Pfadtrick greift nicht", r.status_code in (400, 404), r.status_code)

r = c.get("/api/daten")
d = r.get_json()
check("Daten kommen vollstaendig zurueck",
      len(d["ha"]) == 1 and len(d["tests"]) == 1 and d["cfg"]["erklaerstufe"] == "einfach",
      {"ha": len(d["ha"]), "tests": len(d["tests"])})
check("Ziele ueberleben die Runde", d["cfg"]["ziele"]["Mathematik"] == 2)

r = c.put("/api/hausaufgabe/h1", json={"id": "h1", "fach": "Mathematik", "titel": "S. 42", "erledigt": True})
d = c.get("/api/daten").get_json()
check("Aendern legt keinen zweiten Eintrag an", len(d["ha"]) == 1, len(d["ha"]))
check("Aenderung ist drin", d["ha"][0]["erledigt"] is True)

r = c.delete("/api/hausaufgabe/h1")
d = c.get("/api/daten").get_json()
check("Loeschen entfernt den Eintrag", r.status_code == 200 and len(d["ha"]) == 0, len(d["ha"]))

gross = {"id": "gross", "text": "x" * (app.MAX_EINTRAG_BYTES + 100)}
r = c.put("/api/test/gross", json=gross)
check("Zu grosser Eintrag wird abgelehnt", r.status_code == 400, r.status_code)

# ---- Fremde Daten bleiben fremd ------------------------------------------
c2 = neuer_client()
c2.post("/api/konto", json={"name": "lena", "passwort": "auchgeheim9"})
d2 = c2.get("/api/daten").get_json()
check("Neues Konto sieht nichts vom anderen", len(d2["ha"]) == 0 and len(d2["tests"]) == 0)
c2.put("/api/test/t1", json={"id": "t1", "fach": "Kunst", "titel": "Nur von Lena", "themen": []})
d1 = c.get("/api/daten").get_json()
check("Gleicher Schluessel, getrennte Konten",
      len(d1["tests"]) == 1 and d1["tests"][0]["fach"] == "Biologie", d1["tests"][0]["fach"])

# ---- Anmelden und abmelden -----------------------------------------------
c3 = neuer_client()
r = c3.post("/api/anmelden", json={"name": "nik", "passwort": "falschfalsch"})
check("Falsches Passwort wird abgelehnt", r.status_code == 401, r.status_code)
r = c3.post("/api/anmelden", json={"name": "gibtesnicht", "passwort": "egalegal1"})
check("Unbekannter Name gibt dieselbe Antwort", r.status_code == 401, r.status_code)

r = c3.post("/api/anmelden", json={"name": "NiK", "passwort": "geheim1234"})
check("Anmelden geht auch mit anderer Schreibweise", r.status_code == 200, r.status_code)
d3 = c3.get("/api/daten").get_json()
check("Nach dem Anmelden sind die Daten da", len(d3["tests"]) == 1, len(d3["tests"]))

r = c3.post("/api/passwort", json={"alt": "stimmtnicht", "neu": "nochgeheimer1"})
check("Passwortwechsel ohne altes Passwort scheitert", r.status_code == 403, r.status_code)
r = c3.post("/api/passwort", json={"alt": "geheim1234", "neu": "nochgeheimer1"})
check("Passwortwechsel klappt", r.status_code == 200, r.status_code)
c4 = neuer_client()
r = c4.post("/api/anmelden", json={"name": "nik", "passwort": "nochgeheimer1"})
check("Neues Passwort gilt", r.status_code == 200, r.status_code)
r = c4.post("/api/anmelden", json={"name": "nik", "passwort": "geheim1234"})
check("Altes Passwort gilt nicht mehr", r.status_code == 401, r.status_code)

c3.post("/api/abmelden")
r = c3.get("/api/daten")
check("Nach dem Abmelden ist zu", r.status_code == 401, r.status_code)

# ---- Bremse gegen Durchprobieren ----------------------------------------
app._fehlversuche.clear()
c5 = neuer_client()
for _ in range(app.MAX_FEHLVERSUCHE):
    c5.post("/api/anmelden", json={"name": "lena", "passwort": "immerfalsch"})
r = c5.post("/api/anmelden", json={"name": "lena", "passwort": "auchgeheim9"})
check("Nach zu vielen Versuchen wird gebremst", r.status_code == 429, r.status_code)
app._fehlversuche.clear()
r = c5.post("/api/anmelden", json={"name": "lena", "passwort": "auchgeheim9"})
check("Nach Ablauf der Sperre geht es wieder", r.status_code == 200, r.status_code)

# ---- Alles loeschen ------------------------------------------------------
r = c4.delete("/api/alles")
d4 = c4.get("/api/daten").get_json()
check("Alles loeschen leert die Daten", r.status_code == 200 and len(d4["tests"]) == 0, len(d4["tests"]))
r = c4.get("/api/ich")
check("Das Konto bleibt bestehen", r.get_json()["angemeldet"] is True)

# ---- Zustand -------------------------------------------------------------
r = c.get("/diag")
j = r.get_json()
check("/diag meldet sqlite und ein bereites Schema", j["storage"] == "sqlite" and j["ok"] is True, j)

print()
if failed:
    print("%d von %d Pruefungen fehlgeschlagen:" % (len(failed), len(failed) + 0))
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print("Alle Pruefungen bestanden.")
