"""Claude-Aufrufe fuer den Schulplaner.

Die Seite im Browser darf Claude nicht selbst rufen, sonst muesste der
API-Schluessel mit ausgeliefert werden und jeder koennte ihn auslesen und auf
fremde Rechnung benutzen. Also geht jeder Aufruf ueber diesen Server: das
Geraet schickt die Anfrage hierher, hier liegt der Schluessel, und nur die
fertige Antwort geht zurueck.

Ohne ANTHROPIC_API_KEY ist alles hier abgeschaltet. Die Oberflaeche merkt das
und blendet die betroffenen Knoepfe aus, statt Fehler zu zeigen.

Zum Pruefen ohne Schluessel und ohne Kosten: KI_TESTMODUS=1 setzen. Dann
kommen feste Antworten zurueck, die Ablaeufe lassen sich also durchspielen.
"""
import base64
import json
import os
import re

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Vorgabe ist das staerkste Modell. Wer sparen will, setzt KI_MODELL auf
# claude-sonnet-5 oder claude-haiku-4-5; das ist eine Kostenentscheidung und
# gehoert deshalb dem Betreiber, nicht ins Programm.
KI_MODELL = os.environ.get("KI_MODELL", "claude-opus-5")
TESTMODUS = os.environ.get("KI_TESTMODUS") == "1"

# Cloudflare vor Render bricht sehr lange Anfragen ab. Lieber hier vorher
# aufgeben und eine saubere Meldung zeigen als eine abgeschnittene Verbindung.
TIMEOUT_S = 75.0
MAX_TOKENS_TEXT = 4000
MAX_TOKENS_JSON = 8000
MAX_PROMPT_ZEICHEN = 24000
MAX_BILDER = 4
MAX_BILD_BYTES = 5 * 1024 * 1024
ERLAUBTE_TYPEN = ("image/jpeg", "image/png", "image/webp", "image/gif")

_client = None


class _Log:
    """Winziger Ersatz fuer einen Logger, damit ki.py nichts von app.py braucht."""

    @staticmethod
    def logger_warnung(text):
        import logging
        logging.getLogger("schulplaner.ki").warning("%s", text)


app = _Log()


def verfuegbar():
    return bool(ANTHROPIC_API_KEY) or TESTMODUS


def grenzen():
    """Was die Oberflaeche ueber diesen Server wissen muss."""
    return {
        "aktiv": verfuegbar(),
        "paket": TESTMODUS or paket_da(),
        "modell": KI_MODELL if not TESTMODUS else "testmodus",
        "maxZeichen": MAX_PROMPT_ZEICHEN,
        "bilder": {"maxCount": MAX_BILDER, "maxBytes": MAX_BILD_BYTES,
                   "mediaTypes": list(ERLAUBTE_TYPEN)},
    }


def paket_da():
    """Ob die Bibliothek installiert ist. Wird auch von /diag abgefragt."""
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def client():
    global _client
    if _client is None:
        _client = _anthropic().Anthropic(api_key=ANTHROPIC_API_KEY, timeout=TIMEOUT_S,
                                         max_retries=1)
    return _client


def _anthropic():
    """Laedt die Bibliothek und macht aus einem fehlenden Paket eine Meldung.

    Der Import steht absichtlich nicht oben in der Datei: ohne Schluessel wird
    er nie gebraucht. Der Preis dafuer ist, dass ein fehlendes Paket erst beim
    ersten Aufruf auffaellt - deshalb hier eine klare Meldung statt eines
    Absturzes, und deshalb meldet /diag zusaetzlich, ob das Paket da ist.
    """
    try:
        import anthropic
        return anthropic
    except ImportError:
        raise KiFehler(
            "Auf dem Server fehlt das Paket anthropic. In requirements.txt "
            "eintragen und neu deployen.", "kein_paket", 503)


class KiFehler(Exception):
    """Fehler, dessen Text der Oberflaeche gezeigt werden darf."""

    def __init__(self, text, code="fehler", status=502):
        super().__init__(text)
        self.text = text
        self.code = code
        self.status = status


def _bilder_pruefen(bilder):
    """Nimmt data-URLs oder rohes Base64 und macht Bildbloecke daraus."""
    bloecke = []
    if not bilder:
        return bloecke
    if len(bilder) > MAX_BILDER:
        raise KiFehler("Hoechstens %d Bilder auf einmal." % MAX_BILDER, "zu_viele_bilder", 400)
    for b in bilder:
        if not isinstance(b, str):
            raise KiFehler("Bild hat das falsche Format.", "bild_format", 400)
        typ = "image/jpeg"
        daten = b
        m = re.match(r"^data:([-\w.+/]+);base64,(.*)$", b, re.S)
        if m:
            typ, daten = m.group(1), m.group(2)
        daten = re.sub(r"\s+", "", daten)
        if typ not in ERLAUBTE_TYPEN:
            raise KiFehler("Dieses Bildformat geht nicht: " + typ, "bild_typ", 400)
        try:
            roh = base64.b64decode(daten, validate=True)
        except Exception:
            raise KiFehler("Das Bild war nicht lesbar.", "bild_defekt", 400)
        if len(roh) > MAX_BILD_BYTES:
            raise KiFehler("Das Bild ist zu gross.", "bild_gross", 400)
        bloecke.append({"type": "image", "source": {"type": "base64", "media_type": typ, "data": daten}})
    return bloecke


def _aufrufen(prompt, bilder, effort, max_tokens):
    if not isinstance(prompt, str) or not prompt.strip():
        raise KiFehler("Leere Anfrage.", "leer", 400)
    if len(prompt) > MAX_PROMPT_ZEICHEN:
        raise KiFehler("Die Anfrage ist zu lang.", "zu_lang", 400)
    if effort not in ("low", "medium", "high"):
        effort = "medium"

    inhalt = _bilder_pruefen(bilder)
    inhalt.append({"type": "text", "text": prompt})

    if TESTMODUS:
        return _testantwort(prompt)

    anthropic = _anthropic()
    nachricht = {"role": "user", "content": inhalt}
    try:
        try:
            antwort = client().messages.create(
                model=KI_MODELL,
                max_tokens=max_tokens,
                output_config={"effort": effort},
                messages=[nachricht],
            )
        except anthropic.BadRequestError as exc:
            # output_config steuert, wie gruendlich Claude nachdenkt, und spart
            # damit Geld. Lehnt das Konto oder das Modell den Parameter ab,
            # ist das kein Grund, die ganze Funktion sterben zu lassen -
            # einmal ohne ihn nachfassen und weitermachen.
            if "output_config" not in str(exc) and "effort" not in str(exc):
                raise
            app.logger_warnung("output_config abgelehnt, es geht ohne weiter: %s" % exc)
            antwort = client().messages.create(
                model=KI_MODELL,
                max_tokens=max_tokens,
                messages=[nachricht],
            )
    except anthropic.AuthenticationError:
        raise KiFehler("Der API-Schluessel stimmt nicht.", "schluessel", 502)
    except anthropic.PermissionDeniedError:
        raise KiFehler("Der Schluessel darf dieses Modell nicht benutzen.", "verboten", 502)
    except anthropic.NotFoundError:
        raise KiFehler("Das Modell %s gibt es nicht." % KI_MODELL, "modell", 502)
    except anthropic.RateLimitError:
        raise KiFehler("Gerade zu viele Anfragen. Warte einen Moment.", "rate_limit", 429)
    except anthropic.APITimeoutError:
        raise KiFehler("Claude hat zu lange gebraucht. Versuch es nochmal.", "timeout", 504)
    except anthropic.APIConnectionError:
        raise KiFehler("Keine Verbindung zu Claude.", "verbindung", 502)
    except anthropic.APIStatusError as exc:
        raise KiFehler("Claude hat mit einem Fehler geantwortet (%s)." % exc.status_code, "api", 502)

    if antwort.stop_reason == "refusal":
        raise KiFehler("Claude hat die Anfrage abgelehnt.", "abgelehnt", 400)

    text = "".join(b.text for b in antwort.content if b.type == "text").strip()
    if not text:
        raise KiFehler("Claude hat nichts geantwortet.", "leer_zurueck", 502)
    return text


def _testantwort(prompt):
    """Feste Antworten fuer den Testmodus, damit die Ablaeufe pruefbar sind."""
    if "Multiple-Choice" in prompt:
        return json.dumps({"fragen": [
            {"frage": "Testfrage %d?" % (i + 1), "optionen": ["A", "B", "C", "D"],
             "richtig": i % 4, "erklaerung": "Weil das so ist.", "thema": "Testthema"}
            for i in range(8)]}, ensure_ascii=False)
    if "Karteikarten" in prompt:
        return json.dumps({"karten": [
            {"frage": "Testkarte %d?" % (i + 1), "antwort": "Antwort %d." % (i + 1), "thema": "Testthema"}
            for i in range(14)]}, ensure_ascii=False)
    if "Zerlege sie" in prompt:
        return json.dumps({"fach": "Mathematik", "titel": "Buch S. 42", "faellig": "2026-12-01"},
                          ensure_ascii=False)
    if '"art"' in prompt or "art test" in prompt:
        return json.dumps({"art": "test", "fach": "Mathematik", "titel": "Lineare Funktionen",
                           "datum": "2026-12-01", "themen": [
                               {"titel": "Steigung berechnen", "stichpunkte": ["Steigungsdreieck"]},
                               {"titel": "Nullstellen bestimmen", "stichpunkte": []}]},
                          ensure_ascii=False)
    return ("Das ist eine Testantwort.\n- Erster Punkt\n- Zweiter Punkt\n"
            "Beispiel: 3 mal 4 ist 12.\nMerksatz: Im Testmodus wird nichts abgerechnet.")


def text(prompt, bilder=None, effort="low"):
    return _aufrufen(prompt, bilder, effort, MAX_TOKENS_TEXT)


def wert(prompt, bilder=None, effort="medium"):
    """Antwort als JSON. Liest tolerant, wie es die Oberflaeche erwartet."""
    roh = _aufrufen(prompt, bilder, effort, MAX_TOKENS_JSON)
    try:
        return json.loads(roh)
    except ValueError:
        pass
    zaun = re.search(r"```(?:json)?\s*(.+?)```", roh, re.S)
    if zaun:
        try:
            return json.loads(zaun.group(1).strip())
        except ValueError:
            pass
    for auf, zu in (("{", "}"), ("[", "]")):
        a, b = roh.find(auf), roh.rfind(zu)
        if a >= 0 and b > a:
            try:
                return json.loads(roh[a:b + 1])
            except ValueError:
                continue
    raise KiFehler("Die Antwort war unbrauchbar. Versuch es nochmal.", "kein_json", 502)
