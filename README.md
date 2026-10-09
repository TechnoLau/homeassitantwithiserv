# IServ für Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Diese Custom Integration liest den **Stundenplan** und den **Vertretungsplan**
(inklusive Ausfälle/Entfall) eines IServ-Servers aus und stellt sie als Sensoren
in Home Assistant bereit.

> **Hinweis:** Die Integration nutzt die inoffiziellen Web-Endpunkte
> (`/iserv/app/login`, `/iserv/dieschulapp/api/1.0/current-timetable/`,
> `/iserv/timetable/data`, `/iserv/plan/show/raw`). Sie ist kein offizielles
> Produkt der IServ GmbH.

---

## Funktionen

- Anmeldung mit dem eigenen IServ-Benutzerkonto (Konfiguration über die UI)
- Automatischer Login inkl. CSRF-/Meta-Refresh-Handling und Session-Cookie
- **Ein einziges Gerät "IServ"** – alle Sensoren und Entitäten hängen an diesem
  einen Gerät (kein zweites Gerät, kein doppelter Eintrag)
- Auslesen des Wochen-Stundenplans, der Vertretungen und der Ausfälle
  (aktuelle **und** die zwei folgenden Wochen, damit auch Freitag/Samstag/Sonntag
  und Feiertage den nächsten Schultag korrekt bestimmen)
- Drei Sensoren mit ausführlichen Attributen für Automationen:
  Vertretungen/Ausfälle, Schulbeginn am nächsten Schultag, kompletter Stundenplan
- Fest hinterlegtes **Fallback-Zeitraster** (1.–6. Stunde), falls IServ keine
  Uhrzeiten liefert
- Einstellbares Aktualisierungsintervall (Optionen-Flow)
- Reauth- und Reconfigure-Flow (Passwort ändern, Server wechseln)
- Fallback auf die ältere `/iserv/timetable/data`-API und auf den Raw-Export
  `/iserv/plan/show/raw` (mit IServ-Filterobjekt und deutschen Datumsangaben),
  falls die DieSchulApp-API auf dem Server fehlt
- Mehrere Kurse/Kinder: die DieSchulApp-API wird bei leerer Antwort automatisch
  mit `filterBy=courseSubject.course:in(<Kurs-IDs>)` erneut abgefragt

## Installation

### Über HACS (empfohlen)

1. HACS öffnen → **Integrationen**
2. Drei-Punkte-Menü → **Benutzerdefinierte Repositories**
3. Repository-URL eintragen und als Kategorie **Integration** wählen
4. Integration suchen, installieren und Home Assistant neu starten

### Manuell

1. Ordner `custom_components/iserv` in das Home-Assistant-Konfigurationsverzeichnis kopieren
2. Home Assistant neu starten

**Voraussetzung:** Home Assistant 2024.6 oder neuer.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → IServ**
2. Serveradresse eingeben, z. B. `schule.iserv.de` (mit oder ohne `https://`)
3. IServ-Benutzername und Passwort eingeben

Die Zugangsdaten werden einmalig geprüft; bei falschem Passwort oder nicht
erreichbarem Server zeigt der Dialog eine passende Fehlermeldung.
Das Passwort wird – wie in Home Assistant üblich – im Klartext im Config-Entry
gespeichert. Verwende daher möglichst einen eigenen Account.

## Gerät und Sensoren

Alle Entitäten werden unter **einem** Gerät namens `IServ` angelegt (Geräte-ID
`(iserv, <config_entry_id>)`). Ein zweiter IServ-Account erzeugt ein weiteres
Gerät `IServ` mit den Entities `..._2`.

| Entität | Zustand | Bedeutung |
| --- | --- | --- |
| `sensor.iserv_vertretungen` | Anzahl der Ausfälle + Vertretungen **heute** | Vertretungsplan (Details in den Attributen) |
| `sensor.iserv_schulbeginn` | Uhrzeit `HH:MM` des ersten Unterrichts am **nächsten Schultag** | z. B. `08:10`, bei Ausfall der 1. Stunde `09:00` |
| `sensor.iserv_stundenplan` | Anzahl der Stunden am **angezeigten Schultag** (heute, sonst nächster Schultag) | Wochen-Stundenplan inkl. Status-Marker |

Bei englischer Home-Assistant-Sprache heißen die Entitäten
`sensor.iserv_substitutions`, `sensor.iserv_next_school_start` und
`sensor.iserv_timetable`.

### Fallback-Zeitraster

Liefert IServ keine konkreten Uhrzeiten (oder ist eine Stunde nicht in der
`lessonTimes`-Tabelle enthalten), wird dieses Raster aus `const.py` benutzt:

| Stunde | Beginn | Ende |
| --- | --- | --- |
| 1 | 08:10 | 08:55 |
| 2 | 09:00 | 09:45 |
| 3 | 10:00 | 10:45 |
| 4 | 10:45 | 11:30 |
| 5 | 11:45 | 12:30 |
| 6 | 12:35 | 13:20 |

Echte IServ-Zeiten haben immer Vorrang. Die Endzeiten sind aus der üblichen
Unterrichtslänge von 45 Minuten abgeleitet.

### Attribute `sensor.iserv_vertretungen`

- `count_today` / `count_tomorrow` – Anzahl der Änderungen heute/morgen
- `cancellations` – entfallene Stunden **heute** (mit Lehrer, Raum, Notiz)
- `substitutions` – vertretene Stunden **heute**
- `changes_tomorrow` – alle Änderungen von **morgen**
- `lessons_today` – Anzahl aller geplanten Stunden heute
- `has_changes` / `has_changes_tomorrow` – `true`, wenn es Änderungen gibt
- `date`, `day`, `account`, `attribution`

### Attribute `sensor.iserv_schulbeginn`

- Zustand: `HH:MM` – direkt in Automations-Bedingungen verwendbar
- `timestamp` – ISO-Zeitstempel des Schulbeginns (z. B.
  `2026-09-11T08:10:00+02:00`)
- `date`, `day`, `weekday` – Tag des Schulbeginns
- `period` – Stunde, mit der der Tag beginnt (z. B. `2`)
- `time_source` – `iserv` (echte Uhrzeit) oder `fallback` (Zeitraster)
- `is_today` – `true`, wenn der Schulbeginn noch heute ist
- `skipped_lessons` – die ausgefallenen Stunden vor dem Schulbeginn

Der Sensor liefert den **nächsten** Schulbeginn: solange der erste Unterricht
des Tages noch nicht begonnen hat, ist das der heutige Tag, danach der nächste
Tag mit Unterricht (Wochenenden und komplett ausgefallene Tage werden
übersprungen).

### Attribute `sensor.iserv_stundenplan`

- `date` / `day` / `weekday` – Tag, auf den sich Zustand und `lessons` beziehen
  (heute; ist heute unterrichtsfrei, der nächste Schultag)
- `is_today` – `true`, wenn sich der Zustand auf heute bezieht
- `lessons` – Stundenliste des angezeigten Tages
- `next_lesson` – nächste Stunde, die noch nicht begonnen hat (auch über das
  Wochenende/Feiertage hinweg; ausgefallene Stunden werden übersprungen)
- `days` – Wörterbuch `{"2026-09-11": {"date": ..., "day": "Freitag", "weekday": 4, "lessons": [...]}}`
- `today` / `tomorrow` – Stundenlisten für heute/morgen
- `cancelled` / `substituted` – alle ausgefallenen bzw. vertretenen Stunden
- `school_days` – alle Tage mit Unterricht (ISO-Daten)
- `next_school_day` / `next_school_start` – nächster Schultag und Beginn
- `lessons_total`, `account`, `attribution`

Am Wochenende, an Feiertagen und in den Ferien sind `today` und `days` ggf. leer;
der Sensor zeigt dann automatisch den nächsten Schultag aus den geladenen Wochen.

### Aufbau eines Stunden-Objekts

```json
{
  "date": "2026-09-11",
  "day": "Freitag",
  "weekday": 4,
  "period": 2,
  "start_time": "09:00",
  "end_time": "09:45",
  "subject": "Mathematik",
  "room": "A 204",
  "teacher": "MUS",
  "status": "cancelled",
  "cancelled": true,
  "substituted": false,
  "original_subject": "Mathematik",
  "original_room": "A 204",
  "original_teacher": "MUS",
  "substitute_subject": "",
  "substitute_room": "",
  "substitute_teacher": "",
  "time_source": "fallback",
  "note": "Entfall"
}
```

`status` ist immer einer von `regular`, `substituted` oder `cancelled` und
damit direkt für Lovelace-Karten nutzbar (z. B. `custom:calendar-card`,
Markdown-Karten mit `status == 'cancelled'` → rot/durchgestrichen).

## Beispiele

### Benachrichtigung bei Ausfall/Vertretung

```yaml
automation:
  - alias: "IServ Ausfall melden"
    triggers:
      - trigger: state
        entity_id: sensor.iserv_vertretungen
    conditions:
      - condition: template
        value_template: "{{ state_attr(trigger.entity_id, 'has_changes') }}"
    actions:
      - action: notify.persistent_notification
        data:
          title: "Vertretungsplan"
          message: >-
            {{ state_attr(trigger.entity_id, 'cancellations')
               | map(attribute='subject') | join(', ') }} entfällt.
```

### Smarter Wecker: rechtzeitig vor der ersten Stunde klingeln

`sensor.iserv_schulbeginn` liefert die Uhrzeit der ersten Stunde des nächsten
Schultags – auch wenn die 1. (und 2. …) Stunde ausfällt:

```yaml
automation:
  - alias: "IServ Wecker"
    triggers:
      - trigger: time_pattern
        minutes: "/1"
    conditions:
      - condition: template
        value_template: >-
          {{ states('sensor.iserv_schulbeginn') != 'unknown'
             and states('sensor.iserv_schulbeginn') != 'unavailable'
             and states('sensor.iserv_schulbeginn')|as_timestamp(0)
                 - now()|as_timestamp < 2700
             and now().timestamp() >=
                 state_attr('sensor.iserv_schulbeginn', 'timestamp')|as_timestamp - 1800 }}
    actions:
      - action: media_player.play_media
        target:
          entity_id: media_player.schlafzimmer
        data:
          media_content_id: "https://example.org/wecker.mp3"
          media_content_type: music
```

### Uhrzeit des Schulbeginns als Bedingung nutzen

```yaml
condition:
  - condition: template
    value_template: "{{ states('sensor.iserv_schulbeginn') == '09:00' }}"
```

### Erste Stunde des nächsten Schultags anzeigen

```yaml
template:
  - sensor:
      - name: "Erste Stunde am nächsten Schultag"
        state: >-
          {% set day = state_attr('sensor.iserv_stundenplan', 'next_school_day') %}
          {% set lessons = state_attr('sensor.iserv_stundenplan', 'days')[day]['lessons']
             | rejectattr('cancelled') | list %}
          {{ (lessons | sort(attribute='start_time') | first).subject | default('frei') }}
```

### Lovelace-Markdown-Karte mit Ausfällen (rot/durchgestrichen)

```yaml
type: markdown
content: |
  {% for lesson in state_attr('sensor.iserv_stundenplan', 'today') %}
  {% if lesson.status == 'cancelled' %}
  - ~~{{ lesson.start_time }} {{ lesson.subject }} ({{ lesson.room }})~~ :red_circle:
  {% elif lesson.status == 'substituted' %}
  - {{ lesson.start_time }} {{ lesson.subject }} **Vertretung** bei {{ lesson.teacher }} in {{ lesson.room }}
  {% else %}
  - {{ lesson.start_time }} {{ lesson.subject }} ({{ lesson.room }})
  {% endif %}
  {% endfor %}
```

## Optionen

**Einstellungen → Geräte & Dienste → IServ → Konfigurieren** öffnet den
Options-Flow. Dort lässt sich das Aktualisierungsintervall in Minuten
einstellen (Standard: 30, erlaubt: 5–1440). Nach dem Speichern wird der
Config-Entry automatisch neu geladen.

## Bekannte Einschränkungen

- Es werden die **aktuelle und die folgende Woche** geladen (zwei Requests pro
  Aktualisierung), damit „nächster Schultag“ auch am Wochenende stimmt.
  Feiertage und Ferien tauchen in IServ nicht als Stunden auf, der Sensor
  springt dann automatisch auf den nächsten Tag mit Unterricht.
- Die genaue JSON-Struktur unterscheidet sich je nach IServ-Version und
  installierten Modulen. Die Parser lesen daher mehrere bekannte Feldnamen und
  behandeln fehlende Felder tolerant. Sollten auf dem eigenen Server andere
  Felder verwendet werden, geben die Debug-Logs (`logger: custom_components.iserv`)
  Aufschluss.
- Es wird nur gelesen; es werden keine Daten an Dritte gesendet.
- Bei einer Änderung des Benutzernamens über den Reconfigure-Flow bleibt die
  interne `unique_id` des Eintrags unverändert.
- Der Stundenplan-Sensor markiert die großen Attribute (`days`, `today`,
  `tomorrow`, `cancelled`, `substituted`) als „unrecorded“, damit die
  Recorder-Datenbank nicht unnötig wächst. In Automationen sind sie trotzdem
  jederzeit verfügbar.

## Fehlerbehebung

| Meldung | Ursache/Hilfe |
| --- | --- |
| „Serveradresse ist kein gültiger IServ-Host“ | Hostname prüfen (ohne `/iserv`-Pfad) |
| „IServ hat den Benutzernamen oder das Passwort abgelehnt“ | Zugangsdaten prüfen: Der Benutzername ist bei IServ meist ohne `@schule.de` (nur der Accountname). Zum Test im IServ-Portal im selben Netz anmelden. |
| „Die Anmeldung war erfolgreich, aber der Stundenplan konnte nicht gelesen werden“ | Der Login funktioniert, IServ liefert auf `/iserv/dieschulapp/api/1.0/current-timetable/` und `/iserv/timetable/data` aber kein JSON (z. B. weil das Stundenplan-/„DieSchulApp“-Modul für den Account nicht freigeschaltet ist). Debug-Log prüfen. |
| „Der IServ-Server ist nicht erreichbar“ | Netzwerk/DNS, evtl. nur im Schulnetz bzw. über VPN erreichbar |
| Keine Daten in den Sensoren | Debug-Log aktivieren und den Inhalt der HTTP-Antwort prüfen |

Der Login wird in zwei Schritten geprüft: erst das Absenden des Login-Formulars,
danach ein echter Stundenplan-Abruf. Nur wenn IServ das Formular mit einer
Fehlermeldung (z. B. „Benutzername oder Passwort ist falsch“) erneut ausliefert,
meldet die Integration ein falsches Passwort. Die erkannte IServ-Meldung steht
dann im Protokoll.

Debug-Log:

```yaml
logger:
  default: info
  logs:
    custom_components.iserv: debug
```

Mit aktiviertem Debug-Log protokolliert die Integration jeden Request
(Methode, URL, HTTP-Status), den gefundenen Login-Pfad, die verwendeten
Formular-Felder sowie den Grund einer abgelehnten Anmeldung – ohne Passwörter.

## Lizenz

[MIT](LICENSE) – Nutzung auf eigene Gefahr.

