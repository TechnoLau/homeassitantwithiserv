# IServ für Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Diese Custom Integration liest den **Stundenplan** und den **Vertretungsplan**
(inklusive Ausfälle/Entfall) eines IServ-Servers aus und stellt sie als Sensoren
in Home Assistant bereit.

> **Hinweis:** Die Integration nutzt die inoffiziellen Web-Endpunkte
> (`/iserv/app/login`, `/iserv/dieschulapp/api/1.0/current-timetable/`,
> `/iserv/timetable/data`). Sie ist kein offizielles Produkt der IServ GmbH.

---

## Funktionen

- Anmeldung mit dem eigenen IServ-Benutzerkonto (Konfiguration über die UI)
- Automatischer Login inkl. CSRF-/Meta-Refresh-Handling und Session-Cookie
- Auslesen des Wochen-Stundenplans und der Vertretungen/Ausfälle
- Zwei Sensoren mit ausführlichen Attributen für Automationen
- Einstellbares Aktualisierungsintervall (Optionen-Flow)
- Reauth- und Reconfigure-Flow (Passwort ändern, Server wechseln)
- Fallback auf die ältere `/iserv/timetable/data`-API, falls die
  DieSchulApp-API auf dem Server fehlt

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

## Sensoren

| Entität | Zustand | Beschreibung |
| --- | --- | --- |
| `sensor.<name>_stundenplan` | Anzahl der Stunden **heute** | Stundenplan der aktuellen Woche |
| `sensor.<name>_vertretungsplan` | Anzahl der Änderungen (Ausfälle + Vertretungen) **ab heute** | Vertretungsplan der aktuellen Woche |

`<name>` leitet sich aus dem Gerätenamen ab, also z. B.
`sensor.iserv_maxmustermann_stundenplan`.

### Attribute `sensor.*_stundenplan`

- `lessons` – alle Stunden der Woche (Liste von Objekten, siehe unten)
- `today` – nur die Stunden von heute
- `next_lesson` – die nächste noch laufende/kommende Stunde von heute (oder `null`)

### Attribute `sensor.*_vertretungsplan`

- `cancellations` – entfallene Stunden ab heute
- `substitutions` – vertretene/geänderte Stunden ab heute
- `today` – Änderungen von heute
- `has_changes` – `true`, wenn es Ausfälle oder Vertretungen gibt

### Aufbau eines Stunden-Objekts

```json
{
  "day": "Montag",
  "weekday": 0,
  "start_time": "08:00",
  "end_time": "08:45",
  "subject": "Mathematik",
  "room": "A 204",
  "teacher": "MUS",
  "canceled": false,
  "substitution": true,
  "note": "Vertretung"
}
```

## Beispiele

### Benachrichtigung bei Ausfall am nächsten Schultag

```yaml
automation:
  - alias: "IServ Ausfall melden"
    triggers:
      - trigger: state
        entity_id: sensor.iserv_maxmustermann_vertretungsplan
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

### Erste Stunde von morgen anzeigen

```yaml
template:
  - sensor:
      - name: "Erste Stunde morgen"
        state: >-
          {% set lessons = state_attr('sensor.iserv_maxmustermann_stundenplan', 'lessons')
             | selectattr('weekday', 'eq', (now().weekday() + 1) % 7) | list %}
          {{ (lessons | sort(attribute='start_time') | first).subject | default('frei') }}
```

## Optionen

**Einstellungen → Geräte & Dienste → IServ → Konfigurieren** öffnet den
Options-Flow. Dort lässt sich das Aktualisierungsintervall in Minuten
einstellen (Standard: 30, erlaubt: 5–1440). Nach dem Speichern wird der
Config-Entry automatisch neu geladen.

## Bekannte Einschränkungen

- Nur der Wochen-Stundenplan/-Vertretungsplan der **aktuellen** Woche wird
  geladen (Koordinatorensicht: `week_offset = 0` in `IServApiClient.async_get_lessons`).
- Die genaue JSON-Struktur unterscheidet sich je nach IServ-Version und
  installierten Modulen. Die Parser lesen daher mehrere bekannte Feldnamen und
  behandeln fehlende Felder tolerant. Sollten auf dem eigenen Server andere
  Felder verwendet werden, geben die Debug-Logs (`logger: custom_components.iserv`)
  Aufschluss.
- Es wird nur gelesen; es werden keine Daten an Dritte gesendet.
- Bei einer Änderung des Benutzernamens über den Reconfigure-Flow bleibt die
  interne `unique_id` des Eintrags unverändert.

## Fehlerbehebung

| Meldung | Ursache/Hilfe |
| --- | --- |
| „Serveradresse ist kein gültiger IServ-Host“ | Hostname prüfen (ohne `/iserv`-Pfad) |
| „Benutzername oder Passwort ist falsch“ | Zugangsdaten prüfen, ggf. im IServ-Portal anmelden |
| „Der IServ-Server ist nicht erreichbar“ | Netzwerk/DNS, evtl. nur im Schulnetz erreichbar |
| Keine Daten in den Sensoren | Debug-Log aktivieren und den Inhalt der HTTP-Antwort prüfen |

Debug-Log:

```yaml
logger:
  default: info
  logs:
    custom_components.iserv: debug
```

## Lizenz

[MIT](LICENSE) – Nutzung auf eigene Gefahr.

