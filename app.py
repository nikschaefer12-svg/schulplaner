"""
Schulplaner — Server und Website.

Ein kleiner Flask-Dienst, der die Daten eines Schuelers haelt: Hausaufgaben,
Tests, Faecher, Stundenplan und Einstellungen. Die Oberflaeche ist eine
einzelne Seite, die unter / ausgeliefert wird.

Anders als beim Kill-ELO-Backend ist hier nichts oeffentlich. Jeder Zugriff
braucht eine Anmeldung, denn auf dieser Seite stehen Hausaufgaben und Noten.

Endpunkte:
  GET  /                                  -> die Seite
  POST /api/konto        {name, passwort} -> Konto anlegen
  POST /api/anmelden     {name, passwort} -> anmelden, setzt das Sitzungs-Cookie
  POST /api/abmelden                      -> abmelden
  GET  /api/ich                           -> wer angemeldet ist
  GET  /api/daten                         -> alles zum angemeldeten Konto
  PUT  /api/profil       {klasse, ...}    -> Klassenstufe und Schulform
  PUT  /api/cfg | /api/faecher | /api/plan
  PUT  /api/hausaufgabe/<id> | DELETE
  PUT  /api/test/<id>        | DELETE
  GET  /health                            -> "ok", auch ohne Datenbank
  GET  /diag                              -> Zustand der Datenbank
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing

from flask import Flask, g, jsonify, request, send_from_directory, session

DB_PATH = os.environ.get("SCHULPLANER_DB", "schulplaner.db")

# Ist DATABASE_URL gesetzt, laeuft alles auf Postgres, sonst auf SQLite.
# Grund: kostenlose Hoster wie Render haben kein bleibendes Dateisystem, eine
# SQLite-Datei waere dort nach jedem Neustart leer. Lokal ist SQLite die
# einfachere Wahl, deshalb kann der Dienst beides.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_CONNECT_TIMEOUT_S = 10
SCHEMA_RETRY_S = 30.0

# Das Passwort wird nie im Klartext gespeichert, sondern als PBKDF2-Hash mit
# eigenem Salz pro Konto. 210000 Runden sind die aktuelle Empfehlung des OWASP
# fuer PBKDF2 mit SHA-256; hoeher kostet auf dem kleinen Render-Plan zu viel.
PBKDF2_RUNDEN = 210_000
SALZ_BYTES = 16

# Ohne festen Schluessel wird beim Start einer erzeugt. Dann sind nach jedem
# Neustart alle Sitzungen ungueltig und man muss sich neu anmelden. Auf Render
# also SCHULPLANER_SECRET setzen.
SECRET = os.environ.get("SCHULPLANER_SECRET", "")
SECRET_ZUFAELLIG = not SECRET

# Einfache Bremse gegen das Durchprobieren von Passwoertern. Bewusst im
# Speicher und nicht in der Datenbank: es muss nur einen Neustart lang halten,
# und eine Datenbankschreibung pro Fehlversuch waere teurer als der Nutzen.
MAX_FEHLVERSUCHE = 10
SPERRE_S = 15 * 60
_fehlversuche = {}

NAME_MUSTER = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
ARTEN = ("ha", "test", "cfg", "faecher", "plan")
# Obergrenzen, damit ein einzelnes Konto den Speicher nicht sprengt.
MAX_EINTRAG_BYTES = 256 * 1024
MAX_EINTRAEGE_PRO_ART = 2000

app = Flask(__name__)
app.secret_key = SECRET or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Auf Render laeuft alles ueber https, lokal nicht. Ein Secure-Cookie waere
    # auf http://localhost unsichtbar, dann kaeme man lokal nie hinein.
    SESSION_COOKIE_SECURE=bool(DATABASE_URL) or os.environ.get("SCHULPLANER_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


# ---- Datenbank -----------------------------------------------------------

def connect():
    if USE_PG:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError(
                "DATABASE_URL ist gesetzt, aber psycopg fehlt - "
                "'pip install psycopg[binary]' ausfuehren."
            )
        # connect_timeout ist Pflicht: ohne ihn wartet psycopg unbegrenzt. Ist
        # die Datenbank nicht erreichbar, haengt sonst der Arbeitsprozess fest
        # und der Dienst antwortet auf gar nichts mehr.
        return psycopg.connect(DATABASE_URL, row_factory=dict_row,
                               connect_timeout=DB_CONNECT_TIMEOUT_S)
    d = sqlite3.connect(DB_PATH)
    d.row_factory = sqlite3.Row
    return d


def sql(text):
    """Uebersetzt die SQLite-Schreibweise in die von Postgres.

    Beide Treiber koennen Platzhalter, nur mit anderem Zeichen. Alles Weitere
    ist so geschrieben, dass es in beiden Dialekten gilt.
    """
    return text.replace("?", "%s") if USE_PG else text


def ex(d, text, args=()):
    return d.execute(sql(text), tuple(args))


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    ts = "DOUBLE PRECISION" if USE_PG else "REAL"
    pk = "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    schema = [
        f"""
        CREATE TABLE IF NOT EXISTS konten (
            id {pk},
            name TEXT NOT NULL UNIQUE,
            pw_hash TEXT NOT NULL,
            pw_salz TEXT NOT NULL,
            runden INTEGER NOT NULL DEFAULT {PBKDF2_RUNDEN},
            klasse TEXT NOT NULL DEFAULT '',
            schulform TEXT NOT NULL DEFAULT '',
            land TEXT NOT NULL DEFAULT '',
            farbe INTEGER NOT NULL DEFAULT 214,
            erstellt {ts} NOT NULL DEFAULT 0,
            gesehen {ts} NOT NULL DEFAULT 0
        )
        """,
        # Ein Eintrag je Gegenstand. Die Art trennt Hausaufgaben, Tests und die
        # drei Einzeldokumente; der Inhalt ist das JSON, das die Seite ohnehin
        # schon benutzt. So muss das Schema nicht jedes Mal mitwachsen, wenn in
        # der Oberflaeche ein Feld dazukommt.
        f"""
        CREATE TABLE IF NOT EXISTS eintraege (
            konto_id BIGINT NOT NULL,
            art TEXT NOT NULL,
            schluessel TEXT NOT NULL,
            inhalt TEXT NOT NULL,
            geaendert {ts} NOT NULL DEFAULT 0,
            PRIMARY KEY (konto_id, art, schluessel)
        )
        """,
        "CREATE INDEX IF NOT EXISTS eintraege_konto ON eintraege (konto_id, art)",
    ]
    with closing(connect()) as d:
        for statement in schema:
            d.execute(statement)
        d.commit()
    global _schema_ready
    _schema_ready = True


_schema_ready = False
_schema_next_try = 0.0


def ensure_schema():
    """Legt das Schema an, sobald die Datenbank erreichbar ist.

    Beim Start darf das scheitern: kostenlose Postgres-Anbieter fahren die
    Datenbank bei Leerlauf herunter, die erste Verbindung laeuft dann in einen
    Zeitfehler. Der Dienst stirbt daran nicht, versucht es aber mit Abstand
    erneut, sonst wartet jede Anfrage aufs Neue.
    """
    global _schema_next_try
    if _schema_ready or time.time() < _schema_next_try:
        return
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - Grund wird geloggt, Dienst laeuft weiter
        _schema_next_try = time.time() + SCHEMA_RETRY_S
        app.logger.warning("Schema noch nicht bereit: %s", exc)


@app.before_request
def _before(*_args):
    # /health muss ohne Datenbank antworten. Genau dann will man es lesen:
    # wenn etwas klemmt und die Frage lautet, ob ueberhaupt jemand da ist.
    if request.path in ("/health", "/manifest.webmanifest") or request.path.startswith("/static/"):
        return
    ensure_schema()


# ---- Passwoerter und Anmeldung -------------------------------------------

def hash_passwort(passwort, salz, runden=PBKDF2_RUNDEN):
    return hashlib.pbkdf2_hmac("sha256", passwort.encode("utf-8"),
                               bytes.fromhex(salz), runden).hex()


def passwort_pruefen(konto, passwort):
    erwartet = konto["pw_hash"]
    gerechnet = hash_passwort(passwort, konto["pw_salz"], int(konto["runden"]))
    # compare_digest statt ==, damit die Laufzeit nichts ueber den Hash verraet.
    return hmac.compare_digest(erwartet, gerechnet)


def gesperrt(name):
    eintrag = _fehlversuche.get(name.lower())
    if not eintrag:
        return 0
    anzahl, bis = eintrag
    if time.time() > bis:
        _fehlversuche.pop(name.lower(), None)
        return 0
    return max(0, int(bis - time.time())) if anzahl >= MAX_FEHLVERSUCHE else 0


def fehlversuch(name):
    schluessel = name.lower()
    anzahl, bis = _fehlversuche.get(schluessel, (0, 0))
    if time.time() > bis:
        anzahl = 0
    _fehlversuche[schluessel] = (anzahl + 1, time.time() + SPERRE_S)


def konto_laden(d, name=None, konto_id=None):
    if konto_id is not None:
        return ex(d, "SELECT * FROM konten WHERE id = ?", (konto_id,)).fetchone()
    # Namen werden ohne Ruecksicht auf Gross- und Kleinschreibung verglichen,
    # sonst legt man aus Versehen "Nik" und "nik" nebeneinander an.
    return ex(d, "SELECT * FROM konten WHERE LOWER(name) = ?", (name.lower(),)).fetchone()


def angemeldet():
    kid = session.get("konto_id")
    if not kid:
        return None
    row = konto_laden(db(), konto_id=kid)
    if row is None:
        session.clear()
    return row


def konto_json(row):
    return {
        "id": row["id"], "name": row["name"], "klasse": row["klasse"],
        "schulform": row["schulform"], "land": row["land"], "farbe": row["farbe"],
        "eingerichtet": bool(row["klasse"]),
    }


def json_koerper():
    daten = request.get_json(silent=True)
    return daten if isinstance(daten, dict) else {}


@app.post("/api/konto")
def konto_anlegen():
    daten = json_koerper()
    name = str(daten.get("name", "")).strip()
    passwort = str(daten.get("passwort", ""))
    if not NAME_MUSTER.match(name):
        return jsonify(fehler="Der Name braucht 3 bis 32 Zeichen, erlaubt sind Buchstaben, Ziffern, Punkt, Strich und Unterstrich."), 400
    if len(passwort) < 8:
        return jsonify(fehler="Das Passwort braucht mindestens 8 Zeichen."), 400
    if len(passwort) > 256:
        return jsonify(fehler="Das Passwort ist zu lang."), 400
    d = db()
    if konto_laden(d, name=name) is not None:
        return jsonify(fehler="Den Namen gibt es schon. Nimm einen anderen."), 409
    salz = secrets.token_hex(SALZ_BYTES)
    jetzt = time.time()
    ex(d, """INSERT INTO konten (name, pw_hash, pw_salz, runden, erstellt, gesehen)
             VALUES (?, ?, ?, ?, ?, ?)""",
       (name, hash_passwort(passwort, salz), salz, PBKDF2_RUNDEN, jetzt, jetzt))
    d.commit()
    row = konto_laden(d, name=name)
    session.permanent = True
    session["konto_id"] = row["id"]
    return jsonify(konto=konto_json(row)), 201


@app.post("/api/anmelden")
def anmelden():
    daten = json_koerper()
    name = str(daten.get("name", "")).strip()
    passwort = str(daten.get("passwort", ""))
    if not name or not passwort:
        return jsonify(fehler="Name und Passwort fehlen."), 400
    rest = gesperrt(name)
    if rest:
        return jsonify(fehler="Zu viele Versuche. Probier es in %d Minuten wieder." % max(1, rest // 60)), 429
    row = konto_laden(db(), name=name)
    # Bei falschem Namen und falschem Passwort dieselbe Antwort, sonst verraet
    # die Seite, welche Namen es gibt.
    if row is None or not passwort_pruefen(row, passwort):
        fehlversuch(name)
        return jsonify(fehler="Name oder Passwort stimmt nicht."), 401
    _fehlversuche.pop(name.lower(), None)
    ex(db(), "UPDATE konten SET gesehen = ? WHERE id = ?", (time.time(), row["id"]))
    db().commit()
    session.permanent = True
    session["konto_id"] = row["id"]
    return jsonify(konto=konto_json(row))


@app.post("/api/abmelden")
def abmelden():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/ich")
def ich():
    row = angemeldet()
    if row is None:
        return jsonify(angemeldet=False)
    return jsonify(angemeldet=True, konto=konto_json(row))


@app.put("/api/profil")
def profil_speichern():
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    daten = json_koerper()
    klasse = str(daten.get("klasse", row["klasse"]))[:4]
    schulform = str(daten.get("schulform", row["schulform"]))[:64]
    land = str(daten.get("land", row["land"]))[:64]
    try:
        farbe = int(daten.get("farbe", row["farbe"])) % 360
    except (TypeError, ValueError):
        farbe = row["farbe"]
    ex(db(), "UPDATE konten SET klasse = ?, schulform = ?, land = ?, farbe = ? WHERE id = ?",
       (klasse, schulform, land, farbe, row["id"]))
    db().commit()
    return jsonify(konto=konto_json(konto_laden(db(), konto_id=row["id"])))


@app.post("/api/passwort")
def passwort_aendern():
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    daten = json_koerper()
    if not passwort_pruefen(row, str(daten.get("alt", ""))):
        return jsonify(fehler="Das alte Passwort stimmt nicht."), 403
    neu = str(daten.get("neu", ""))
    if len(neu) < 8:
        return jsonify(fehler="Das neue Passwort braucht mindestens 8 Zeichen."), 400
    salz = secrets.token_hex(SALZ_BYTES)
    ex(db(), "UPDATE konten SET pw_hash = ?, pw_salz = ?, runden = ? WHERE id = ?",
       (hash_passwort(neu, salz), salz, PBKDF2_RUNDEN, row["id"]))
    db().commit()
    return jsonify(ok=True)


# ---- Daten ---------------------------------------------------------------

def art_pruefen(art):
    return art in ARTEN


def eintraege_lesen(d, konto_id, art):
    rows = ex(d, "SELECT schluessel, inhalt FROM eintraege WHERE konto_id = ? AND art = ?",
              (konto_id, art)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["inhalt"]))
        except (ValueError, TypeError):
            # Ein kaputter Eintrag darf nicht die ganze Liste unlesbar machen.
            app.logger.warning("Eintrag %s/%s ist kein gueltiges JSON", art, r["schluessel"])
    return out


def eintrag_schreiben(d, konto_id, art, schluessel, wert):
    text = json.dumps(wert, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_EINTRAG_BYTES:
        return False, "Der Eintrag ist zu gross."
    anzahl = ex(d, "SELECT COUNT(*) AS n FROM eintraege WHERE konto_id = ? AND art = ?",
                (konto_id, art)).fetchone()["n"]
    vorhanden = ex(d, "SELECT 1 FROM eintraege WHERE konto_id = ? AND art = ? AND schluessel = ?",
                   (konto_id, art, schluessel)).fetchone() is not None
    if not vorhanden and anzahl >= MAX_EINTRAEGE_PRO_ART:
        return False, "Zu viele Eintraege. Raeum alte auf."
    jetzt = time.time()
    if vorhanden:
        ex(d, "UPDATE eintraege SET inhalt = ?, geaendert = ? WHERE konto_id = ? AND art = ? AND schluessel = ?",
           (text, jetzt, konto_id, art, schluessel))
    else:
        ex(d, "INSERT INTO eintraege (konto_id, art, schluessel, inhalt, geaendert) VALUES (?, ?, ?, ?, ?)",
           (konto_id, art, schluessel, text, jetzt))
    d.commit()
    return True, ""


def einzeln_lesen(d, konto_id, art, standard):
    row = ex(d, "SELECT inhalt FROM eintraege WHERE konto_id = ? AND art = ? AND schluessel = ?",
             (konto_id, art, "x")).fetchone()
    if row is None:
        return standard
    try:
        return json.loads(row["inhalt"])
    except (ValueError, TypeError):
        return standard


@app.get("/api/daten")
def daten_lesen():
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    d = db()
    kid = row["id"]
    return jsonify(
        konto=konto_json(row),
        ha=eintraege_lesen(d, kid, "ha"),
        tests=eintraege_lesen(d, kid, "test"),
        faecher=einzeln_lesen(d, kid, "faecher", []),
        plan=einzeln_lesen(d, kid, "plan", []),
        cfg=einzeln_lesen(d, kid, "cfg", {}),
    )


@app.put("/api/hausaufgabe/<schluessel>")
def hausaufgabe_speichern(schluessel):
    return gegenstand_speichern("ha", schluessel)


@app.put("/api/test/<schluessel>")
def test_speichern(schluessel):
    return gegenstand_speichern("test", schluessel)


def gegenstand_speichern(art, schluessel):
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", schluessel):
        return jsonify(fehler="Ungueltiger Schluessel."), 400
    koerper = json_koerper()
    if not koerper:
        return jsonify(fehler="Leerer Inhalt."), 400
    ok, fehler = eintrag_schreiben(db(), row["id"], art, schluessel, koerper)
    return (jsonify(ok=True), 200) if ok else (jsonify(fehler=fehler), 400)


@app.delete("/api/hausaufgabe/<schluessel>")
def hausaufgabe_loeschen(schluessel):
    return gegenstand_loeschen("ha", schluessel)


@app.delete("/api/test/<schluessel>")
def test_loeschen(schluessel):
    return gegenstand_loeschen("test", schluessel)


def gegenstand_loeschen(art, schluessel):
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    ex(db(), "DELETE FROM eintraege WHERE konto_id = ? AND art = ? AND schluessel = ?",
       (row["id"], art, schluessel))
    db().commit()
    return jsonify(ok=True)


@app.put("/api/<art>")
def einzeln_speichern(art):
    if art not in ("cfg", "faecher", "plan"):
        return jsonify(fehler="Unbekannter Bereich."), 404
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    koerper = request.get_json(silent=True)
    if koerper is None:
        return jsonify(fehler="Leerer Inhalt."), 400
    ok, fehler = eintrag_schreiben(db(), row["id"], art, "x", koerper)
    return (jsonify(ok=True), 200) if ok else (jsonify(fehler=fehler), 400)


@app.delete("/api/alles")
def alles_loeschen():
    """Loescht alle Daten des Kontos, aber nicht das Konto selbst."""
    row = angemeldet()
    if row is None:
        return jsonify(fehler="Nicht angemeldet."), 401
    ex(db(), "DELETE FROM eintraege WHERE konto_id = ?", (row["id"],))
    db().commit()
    return jsonify(ok=True)


# ---- Seite und Zustand ---------------------------------------------------

@app.get("/")
def seite():
    # Die Seite geht bewusst nicht durch Jinja, sondern direkt als Datei raus.
    # Im CSS steht Zeug wie "@media (min-width:900px){#toast{...}}", und "{#"
    # ist fuer Jinja der Anfang eines Kommentars - die Seite waere kaputt.
    # Dynamisch ist an ihr ohnehin nichts, alles kommt ueber /api.
    # max_age=0, damit nach einem Deploy nicht die alte Fassung im Browser haengt.
    return send_from_directory(app.template_folder, "index.html", max_age=0)


@app.get("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest")


@app.get("/health")
def health():
    return "ok", 200


def redact(text):
    """Nimmt Zugangsdaten aus einer Fehlermeldung, bevor sie nach aussen geht."""
    return re.sub(r"(postgres(?:ql)?://)[^@\s]+@", r"\1***@", str(text))


def seiten_stand():
    """Welche Fassung der Oberflaeche liegt gerade da.

    Ohne das raet man beim Deployen: die Seite sieht gleich aus, egal ob der
    neue Stand schon drauf ist. Groesse und Zeitstempel sagen es eindeutig,
    und ob die Texterkennung eingebaut ist, steht direkt dabei.
    """
    pfad = os.path.join(app.template_folder, "index.html")
    try:
        roh = open(pfad, encoding="utf-8").read()
        return {
            "bytes": len(roh.encode("utf-8")),
            "geaendert": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(os.path.getmtime(pfad))),
            "texterkennung": "SCHULPLANER_OCR" in roh,
        }
    except OSError as exc:
        return {"fehler": str(exc)}


@app.get("/diag")
def diag():
    info = {
        "storage": "postgres" if USE_PG else "sqlite",
        "database_url_set": bool(DATABASE_URL),
        "schema_ready": _schema_ready,
        "secret_zufaellig": SECRET_ZUFAELLIG,
        # Render legt den ausgelieferten Commit in diese Variable.
        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
        "seite": seiten_stand(),
    }
    started = time.time()
    try:
        ex(db(), "SELECT 1").fetchone()
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001 - der Grund ist hier der Zweck
        info["ok"] = False
        info["error_type"] = type(exc).__name__
        info["error"] = redact(str(exc))
    info["took_ms"] = int((time.time() - started) * 1000)
    return jsonify(info), 200 if info.get("ok") else 503


# Beim Import wird bewusst nicht verbunden. Sonst haengt der Arbeitsprozess
# schon beim Start an einer Datenbank, die vielleicht gerade hochfaehrt. Das
# Schema entsteht bei der ersten Anfrage, siehe _before.

if __name__ == "__main__":
    if SECRET_ZUFAELLIG:
        print("Hinweis: SCHULPLANER_SECRET ist nicht gesetzt. "
              "Nach einem Neustart muss man sich neu anmelden.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
