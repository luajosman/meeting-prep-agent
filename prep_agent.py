#!/usr/bin/env python3
"""
Meeting-Prep-Agent
==================
Liest ein Google-Calendar-Event, recherchiert Firma + Ansprechpartner,
erzeugt einen Prep-Brief und schreibt ihn zurueck in die Event-Beschreibung.

Nutzung:
    python prep_agent.py --next                 # naechster Termin im Kalender
    python prep_agent.py --next --dry-run       # nur ausgeben, nichts schreiben
    python prep_agent.py --query "WOLFF"        # Termin per Titel-Suche
    python prep_agent.py --event-id <id>
"""

import argparse
import datetime as dt
import html
import os
import re
import sys
import textwrap

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MODEL = os.environ.get("PREP_MODEL", "claude-opus-5")
CALENDAR_ID = os.environ.get("PREP_CALENDAR_ID", "primary")

# Marker, damit wiederholte Laeufe den alten Brief ersetzen statt anzuhaengen.
START_MARK = "===== KURO PREP BRIEF (auto-generiert) ====="
END_MARK = "===== ENDE PREP BRIEF ====="

# Google erlaubt rund 8.000 Zeichen in der Beschreibung.
MAX_DESC_CHARS = 8000


# ----------------------------------------------------------------------
# 1. Kalender
# ----------------------------------------------------------------------

def calendar_service():
    """OAuth-Desktop-Flow. Beim ersten Lauf oeffnet sich der Browser."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def find_event(svc, *, event_id=None, query=None, take_next=False):
    if event_id:
        return svc.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    params = dict(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=20,
        singleEvents=True,
        orderBy="startTime",
    )
    if query:
        params["q"] = query

    items = svc.events().list(**params).execute().get("items", [])
    if not items:
        sys.exit("Kein passender Termin gefunden.")
    if take_next or query:
        return items[0]
    return items[0]


# ----------------------------------------------------------------------
# 2. Event parsen
# ----------------------------------------------------------------------

def html_to_text(raw):
    """
    Google Calendar liefert Beschreibungen als HTML aus, sobald sie im
    Web-UI bearbeitet wurden: Zeilenumbrueche werden zu <br>, Links zu
    <a>-Tags, Sonderzeichen zu Entities. Ohne diese Normalisierung sieht
    der Parser nur eine einzige lange Zeile.
    """
    if not raw:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    t = re.sub(r"</(p|div|li|tr)>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()


def to_html(text):
    """Umgekehrte Richtung fuer das Zurueckschreiben."""
    return html.escape(text).replace("\n", "<br>")


def parse_event(event):
    """
    Zieht Firma, Website, Teilnehmer und Ziel aus dem Event.

    Bewusste Design-Entscheidung: Teilnehmer werden primaer aus der
    BESCHREIBUNG gelesen, nicht aus dem attendees-Feld. So muss niemand
    als echter Gast eingeladen werden. Das entspricht auch der Realitaet:
    Ansprechpartner der Gegenseite stehen oft nur im Beschreibungstext.
    """
    desc = html_to_text(event.get("description", "") or "")
    # Alten Brief ausblenden, damit er nicht als Input zurueckkommt.
    desc = desc.split(START_MARK)[0].strip()

    def field(name):
        m = re.search(rf"^{name}\s*:\s*(.+)$", desc, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ""

    people = []
    block = re.search(
        r"^(?:Teilnehmer|Ansprechpartner)\s*:\s*$(.*?)(?=^\w+\s*:|\Z)",
        desc,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if block:
        for line in block.group(1).splitlines():
            line = line.strip(" -•\t")
            if line:
                people.append(line)

    # Fallback: eingeladene Gaeste (interne Kollegen).
    internal = [
        a.get("email", "")
        for a in event.get("attendees", [])
        if not a.get("self") and not a.get("resource")
    ]

    start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")

    return {
        "title": event.get("summary", "(ohne Titel)"),
        "start": start,
        "company": field("Firma") or field("Company"),
        "website": field("Website"),
        "goal": field("Ziel") or field("Goal"),
        "people": people,
        "internal": internal,
        "raw_notes": desc,
    }


# ----------------------------------------------------------------------
# 3. Recherche + Brief
# ----------------------------------------------------------------------

SYSTEM = """Du bist Research-Assistent fuer das Vertriebsteam von Kuro.

Kuro verkauft ein KI-Projektdatensystem an deutsche Generalunternehmer.
Module: intelligente Dokumentenablage, Projekt- und Bausollanalyse,
Widerspruchsanalyse, KI-Raumbuch, Angebotsauswertung (nicht-GAEB),
automatische UBB-Pruefung. Referenzkunden u.a. Hochtief, Goldbeck,
Koester, Riedel Bau, Geiger, Otto Wulff, Implenia. Pricing ist nicht
nutzerbasiert, sondern nach Projektvolumen bzw. Dokumentenaufkommen.
Einstieg ist typischerweise ein 8-woechiger Proof-of-Value.

Du erstellst einen Prep-Brief fuer ein anstehendes Gespraech.

REGELN ZUR BELEGBARKEIT - das ist der wichtigste Teil:
- Jede Zahl und jede Tatsachenbehauptung braucht eine Quelle.
- Was du nicht belegen kannst, kommt NICHT in die Fakten-Abschnitte,
  sondern in den Abschnitt "Nicht verifiziert".
- Kennzeichne Einschaetzungen und Hypothesen klar als solche.
- Erfinde niemals Namen, Umsatzzahlen, Projekte oder Zitate.
- Zu LinkedIn-Profilen findest du oft nichts Abrufbares. Dann schreibe das
  hin, statt eine Biografie zu konstruieren.

STIL: Deutsch. Dichte Stichpunkte, keine Fliesstextabsaetze. Der Brief wird
in einem Kalender-Event gelesen, also reines Markdown ohne Tabellen.
Maximal 700 Woerter. Lieber weniger und belegt als viel und vage."""


BRIEF_STRUCTURE = """Struktur des Briefs, genau diese Abschnitte:

## Worum es geht
Ein Satz zum Ziel, ein Satz dazu, was ein Erfolg dieses Termins waere.

## Die Firma
Umsatz, Mitarbeiterzahl, Sparten, Regionen, Eigentuemerstruktur,
typische Projektgroessen. Nur mit Quelle.

## Digitalisierungs-Signale
Hinweise auf Kaufbereitschaft: Stellenausschreibungen (BIM, Digitalisierung,
Data), Presse zu Digitalstrategie, erkennbare Bestandssysteme (z.B. RIB iTWO,
Thinkproject, Nemetschek, Autodesk), BIM-Referenzen, Innovationslabs.
Das ist der wichtigste Abschnitt.

## Gespraechspartner
Pro Person: Rolle, Verantwortungsbereich, woran sie vermutlich gemessen wird,
welchen Hebel sie in einer Kaufentscheidung hat. Wenn nichts belegbar ist:
sag das.

## Schmerz-Hypothesen
Zwei bis drei konkrete Hypothesen, jeweils an ein Kuro-Modul gekoppelt.
Als Hypothese formuliert, nicht als Fakt.

## Discovery-Fragen
Fuenf offene Fragen, die zur Firma passen. Keine generischen Fragen,
die man jedem GU stellen koennte.

## Passende Referenzen
Welche Kuro-Referenzkunden oder Proof-Points hier ziehen, und warum genau diese.

## Erwartbare Einwaende
Drei Einwaende mit je einer Antwortlinie.

## Next-Step-Ask
Was am Ende des Termins konkret vereinbart werden soll.

## Quellen
Liste der genutzten URLs.

## Nicht verifiziert
Was du gesucht, aber nicht belegen konntest. Wenn dieser Abschnitt leer
bleibt, hast du etwas falsch gemacht."""


def clean_brief(text):
    """
    Kalender rendert kein Markdown. Ausserdem stellt das Modell der Antwort
    gelegentlich einen Ankuendigungssatz voran ("I'll research X before ...").
    Beides wird hier entfernt, damit im Event lesbarer Klartext steht.
    """
    # Alles vor der ersten Ueberschrift verwerfen. Nicht zeilenweise suchen:
    # die Ankuendigung klebt oft ohne Umbruch an der ersten Ueberschrift.
    idx = text.find("##")
    if idx > 0:
        text = text[idx:]

    out = []
    for line in text.splitlines():
        h = re.match(r"^#{1,6}\s*(.+?)\s*#*$", line)
        if h:
            out.append("")
            out.append(h.group(1).upper())
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)      # fett
        line = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*", r"\1", line)  # kursiv
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)    # Aufzaehlung
        out.append(line.rstrip())

    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def build_brief(client, ctx):
    people = "\n".join(f"- {p}" for p in ctx["people"]) or "(keine im Event hinterlegt)"
    prompt = textwrap.dedent(f"""\
        Termin: {ctx['title']}
        Beginn: {ctx['start']}
        Firma: {ctx['company'] or '(aus Titel ableiten)'}
        Website: {ctx['website'] or '(unbekannt, recherchieren)'}
        Ziel laut Notiz: {ctx['goal'] or '(nicht angegeben)'}

        Gespraechspartner der Gegenseite:
        {people}

        Weitere Notizen im Event:
        {ctx['raw_notes'] or '(keine)'}

        Recherchiere und erstelle den Prep-Brief.

        {BRIEF_STRUCTURE}
        """)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 12,
            "user_location": {
                "type": "approximate",
                "country": "DE",
                "timezone": "Europe/Berlin",
            },
        }],
    )

    text = "".join(b.text for b in resp.content if b.type == "text")
    searches = sum(1 for b in resp.content if b.type == "server_tool_use")
    return clean_brief(text), searches


# ----------------------------------------------------------------------
# 4. Zurueckschreiben
# ----------------------------------------------------------------------

def drop_section(text, heading):
    """Entfernt einen Abschnitt vom Heading bis zur naechsten Ueberschrift."""
    pattern = rf"^{heading}$.*?(?=^[A-ZÄÖÜ][A-ZÄÖÜ \-/()&.]{{3,}}$|\Z)"
    return re.sub(pattern, "", text, flags=re.MULTILINE | re.DOTALL)


def fit(brief, budget):
    """Bei Platzmangel weicht zuerst die Quellenliste, nie 'Nicht verifiziert'."""
    if len(brief) <= budget:
        return brief
    body = drop_section(brief, "QUELLEN").strip()
    body += "\n\n(Quellenliste gekuerzt, vollstaendig in der Terminal-Ausgabe.)"
    if len(body) > budget:
        body = body[:budget] + "\n[gekuerzt]"
    return body


def parse_start(event):
    """
    Liefert den Startzeitpunkt. Ganztaegige Termine haben keine Uhrzeit,
    vor die sich ein Block legen liesse. Statt abzubrechen wird der
    Prep-Block dann auf 9:00 des Tages gelegt - lieber ein Block an
    ungefaehr richtiger Stelle als gar keiner.
    """
    raw = event.get("start", {}).get("dateTime")
    if raw:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")), False

    day = event.get("start", {}).get("date")
    if not day:
        sys.exit("Termin hat weder Datum noch Uhrzeit.")
    start = dt.datetime.fromisoformat(day).replace(hour=9, minute=0)
    return start, True


def push_prep_event(svc, event, brief):
    """
    Schreibt den Brief NICHT in den Kundentermin, sondern in einen eigenen
    Prep-Block 15 Minuten davor.

    Grund: Der Brief ist internes Vertriebsmaterial - Schmerz-Hypothesen,
    Einschaetzungen zum Budgethebel einzelner Personen, vorbereitete
    Antworten auf Einwaende. Gaeste eines Termins sehen dessen Beschreibung.
    Sobald jemand den Kunden zum Folgetermin einlaedt, ginge der Brief mit
    raus. Ein eigenes Event ohne Gaeste macht diesen Fehler strukturell
    unmoeglich, statt ihn nur per Konvention zu verbieten.
    """
    src_start, all_day = parse_start(event)
    tz = event.get("start", {}).get("timeZone") or "Europe/Berlin"
    prep_start = src_start - dt.timedelta(minutes=15)
    if all_day:
        print("→ Hinweis: Termin ist ganztaegig, Prep-Block wird auf 8:45 gelegt.")

    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    body = fit(brief, MAX_DESC_CHARS - 400)
    plain = f"{START_MARK}\nStand: {stamp} | Modell: {MODEL}\n\n{body}\n{END_MARK}"

    payload = {
        "summary": f"Prep: {event.get('summary', 'Termin')}",
        "description": to_html(plain),
        "start": {"dateTime": prep_start.isoformat(), "timeZone": tz},
        "end": {"dateTime": src_start.isoformat(), "timeZone": tz},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 0}]},
        "extendedProperties": {"private": {"kuroPrepFor": event["id"]}},
        "transparency": "transparent",
    }

    # Schon ein Prep-Block fuer diesen Termin da? Dann aktualisieren statt
    # einen zweiten anzulegen.
    existing = svc.events().list(
        calendarId=CALENDAR_ID,
        privateExtendedProperty=f"kuroPrepFor={event['id']}",
        maxResults=1,
    ).execute().get("items", [])

    if existing:
        res = svc.events().patch(
            calendarId=CALENDAR_ID, eventId=existing[0]["id"],
            body=payload, sendUpdates="none").execute()
        return "aktualisiert", res.get("htmlLink", "")

    res = svc.events().insert(
        calendarId=CALENDAR_ID, body=payload, sendUpdates="none").execute()
    return "angelegt", res.get("htmlLink", "")


def push_into_event(svc, event, brief):
    """Alternative mit --in-event: direkt in den Kundentermin schreiben."""
    original = event.get("description", "") or ""
    base = original.split(START_MARK)[0].rstrip()

    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    body = fit(brief, MAX_DESC_CHARS - len(base) - 400)
    plain = f"{START_MARK}\nStand: {stamp} | Modell: {MODEL}\n\n{body}\n{END_MARK}"

    svc.events().patch(
        calendarId=CALENDAR_ID,
        eventId=event["id"],
        body={"description": base + "<br><br>" + to_html(plain)},
        sendUpdates="none",
    ).execute()
    return "in den Termin geschrieben", event.get("htmlLink", "")


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--next", action="store_true", help="naechster Termin")
    ap.add_argument("--query", help="Termin per Titel suchen")
    ap.add_argument("--event-id", help="konkrete Event-ID")
    ap.add_argument("--dry-run", action="store_true", help="nur ausgeben")
    ap.add_argument("--in-event", action="store_true",
                    help="in den Kundentermin schreiben statt in einen eigenen Prep-Block")
    args = ap.parse_args()

    if not (args.next or args.query or args.event_id):
        ap.error("Eins von --next, --query, --event-id angeben.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY ist nicht gesetzt.")

    svc = calendar_service()
    event = find_event(svc, event_id=args.event_id, query=args.query, take_next=args.next)
    ctx = parse_event(event)

    print(f"→ Termin: {ctx['title']} ({ctx['start']})")
    print(f"→ Firma:  {ctx['company'] or '(wird recherchiert)'}")
    print(f"→ Personen: {len(ctx['people'])}")
    print("→ Recherche laeuft ...\n")

    client = anthropic.Anthropic()
    brief, searches = build_brief(client, ctx)

    print(brief)
    print(f"\n→ {searches} Websuchen durchgefuehrt.")

    if args.dry_run:
        print("→ Dry-Run, Kalender unveraendert.")
        return

    if args.in_event:
        action, link = push_into_event(svc, event, brief)
    else:
        action, link = push_prep_event(svc, event, brief)
    print(f"→ Prep-Block {action}: {link}")


if __name__ == "__main__":
    main()
