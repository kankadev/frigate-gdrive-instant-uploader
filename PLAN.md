# PLAN – Verbesserungsideen

Sammlung von Verbesserungen, die das Tool stabiler, schneller oder
ressourcen-schonender machen würden. Reihenfolge nach geschätztem Nutzen.

## 1. Frigate-Reachability-Check vor dem Retry-Job

**Status:** offen
**Priorität:** mittel

Wenn Frigate komplett offline ist (LXC neugestartet, Netzwerk weg), schlagen
alle Retries fehl und produzieren Notifications. Erst ein einmaliger
Health-Check könnte den Job direkt abbrechen.

**Vorschlag:** Am Anfang von `handle_not_uploaded_events()`: ein einziger
`GET /api/version` auf Frigate. Wenn nicht erreichbar → Job überspringen
mit `WARNING`-Log statt 400× Fehler.

**Aufwand:** klein, hoher Nutzen bei Netzwerk-Ausfällen.

---

## 2. Strukturierte Fehlerstatistiken in der DB

**Status:** offen
**Priorität:** niedrig

Aktuell wissen wir nur `tries` und `retry`. Wir wissen aber nicht, an welchem
Schritt es jeweils gescheitert ist (Download? Upload? Auth?).

**Vorschlag:** Spalte `last_error_kind` (z.B. `clip_400`, `frigate_5xx`,
`drive_quota`, `network`). Hilft beim Daily-Report, gezielter zu warnen.

**Aufwand:** mittel (Migration + Code-Anpassungen an mehreren Stellen).

---

## 3. Maximale Event-Dauer für Uploads

**Status:** offen
**Priorität:** mittel

Events mit extrem langen Dauern (z.B. 5+ Stunden bei "stationary objects"
wie schlafende Katzen) erzeugen riesige Clips. Frigate braucht Minuten,
um diese zusammenzusetzen, und blockiert dabei die API. Der Upload-Loop
steht still.

**Vorschlag:** Konfigurierbarer Parameter `SKIP_EVENTS_LONGER_THAN_SECONDS`
(Default z.B. 7200s = 2h). Events, die länger dauern, als `retry=0`
markieren (nicht löschen, damit die Metadaten erhalten bleiben).

**Aufwand:** klein.

**Nutzen:** Loop hängt nicht mehr an 5-Stunden-Events, deutlich schnellerer
Durchsatz bei Backlogs.

---

## 4. Maximale Clip-Größe überspringen (`MAX_CLIP_SIZE`)

**Status:** offen
**Priorität:** mittel

Einige Clips werden riesig (z.B. 10 GB bei 14h-Events). Auch wenn Frigate sie
korrekt assembliert, dauert der Upload ewig und blockiert die Queue. Wenn der
User sowieso keine 10-GB-Videos in Google Drive will, sollten wir sie direkt
ablehnen.

**Vorschlag:** Env-Variable `MAX_CLIP_SIZE` mit human-readable Parser:
- `MAX_CLIP_SIZE=5GB` → überspringe Clips > 5 GB
- `MAX_CLIP_SIZE=500MB` → überspringe Clips > 500 MB
- `MAX_CLIP_SIZE=0` oder leer → keine Begrenzung (Default)

**Implementierung:**
1. Vor Download: `HEAD` Request auf Clip-URL oder `Content-Length` aus erstem
   `GET`-Chunk lesen.
2. Wenn bekannt und > Limit → sofort `retry=0` mit Log:
   `Skipping X GB clip for {event_id}, exceeds MAX_CLIP_SIZE=5GB`
3. Wenn unbekannt (Streaming) → während Download prüfen und abbrechen.

**Aufwand:** klein.
**Nutzen:** 10-GB-Monster-Events werden in 1 Sekunde abgelehnt statt
10 Minuten heruntergeladen zu werden.

---

## 5. `fetch_all_events` als Generator (Streaming)

**Status:** offen
**Priorität:** niedrig

`fetch_all_events()` in `src/frigate_api.py` lädt aktuell ALLE Events
in eine Python-Liste. Bei 24.000+ Events kann das viel Memory fressen.

**Vorschlag:** Funktion in Generator umwandeln (`yield` statt
`all_events.append()`), damit Events als Stream verarbeitet werden
und der Memory-Footprint konstant bleibt.

**Aufwand:** klein (nur `return all_events` → `yield event` pro Batch).

**Nutzen:** konstanter Memory-Verbrauch, unabhängig von Event-Anzahl.

---

## 6. MQTT `on_message` in separaten Thread auslagern

**Status:** offen
**Priorität:** hoch

`on_message` (in `main.py`) ruft direkt `handle_single_event()` auf, was bei einem
10-Minuten-Download den **gesamten MQTT-Client-Thread blockiert**. Der Broker
pingt nicht mehr → Keepalive-Timeout → Disconnect.

**Schnellfix:** keepalive auf 180s erhöht (Done). Das verschiebt das Problem
von ~90s auf ~270s, aber bei riesigen Events (>3h) reicht auch das nicht.

**Richtige Lösung:**
```python
# Statt direktem Aufruf:
handle_single_event(event_data)

# In einen Daemon-Thread auslagern:
threading.Thread(
    target=handle_single_event,
    args=(event_data,),
    daemon=True
).start()
```

Das lässt `on_message` sofort zurückkehren. Der MQTT-Loop kann weiter pingen
und neue Events empfangen.

**Abhängigkeit:** Punkt 7 (Threading / Parallel-Uploads) — die gleichen
SQLite-Concurrency-Probleme gelten hier. WAL-Mode hilft, aber Connection-Sharing
zwischen Threads kann trotzdem zu `database is locked` führen.

**Aufwand:** mittel (Threading + DB-Connection-Handling).
**Nutzen:** Keine MQTT-Disconnects mehr, Events werden sofort empfangen statt
verspätet.

---

## 7. Threading / Parallel-Uploads (mit SQLite-Warnung)

**Status:** offen
**Priorität:** niedrig (erst nach SQLite-Concurrency-Lösung)

Parallele Uploads könnten den Durchsatz massiv erhöhen, da der Upload
meistens I/O-bound ist (Frigate-Download + Google Drive-Upload).

**WICHTIG / WARNUNG:**
Das Tool hatte früher Threading, das wurde aber wieder zurückgebaut, weil
**konkurrierende Zugriffe auf SQLite** zu `database is locked`-Fehlern
führten. Vor einer Wiedereinführung von Threading müssen folgende
Voraussetzungen erfüllt sein:
- SQLite WAL-Mode ist aktiv (✅ bereits erledigt)
- Jeder Thread bekommt eine **eigene DB-Connection** (kein Connection-Sharing)
- ODER: ein **dedizierter DB-Worker-Thread** mit Queue (Producer-Consumer-Muster)
- ODER: Umstieg auf eine echte Concurrency-fähige DB (z.B. PostgreSQL)

**Aufwand:** mittel bis hoch (abhängig von gewählter Architektur).

**Nutzen:** Bei 374+ Backlog-Events wäre Parallel-Upload ein Gamechanger.

---

## Done

- [x] Interval-Jobs starten 90s nach Container-Start (kein 10-Minuten-Blindflug nach Neustart)
- [x] Hard-Fail-Cleanup: 404 auf `/api/events/{id}` → Auto-Delete
- [x] Clip-Cleanup: 404 auf `/clip.mp4` → Auto-Delete
- [x] Konservativer 400-Umgang: kein Auto-Delete (Datenintegrität)
- [x] Einheitliche Retention via `DB_RETENTION_DAYS` (default 30) – ersetzt `EVENT_RETENTION_DAYS` und `STALE_PENDING_DAYS` mit Fallback-Kompatibilität
- [x] Aufgeben nach `MAX_RETRY_ATTEMPTS` Versuchen (default 50)
- [x] Aufnahme-Timestamp in Log-Messages für bessere Lesbarkeit
- [x] Daily Health Report (grün/orange/rot) via Mattermost
- [x] Upload-Lock gegen SSL-Race-Conditions
- [x] SQLite WAL-Mode für Concurrency
- [x] Python 3.12 + gepinnte Dependencies
- [x] **Retry-Queue FIFO:** `ORDER BY created ASC` in `select_not_uploaded_yet()`
- [x] **HTTP 400 "No recordings found" → permanent failure:** `ClipNotAvailableError` + Auto-Delete
- [x] **Job-Reihenfolge getauscht:** `handle_not_uploaded_events()` vor `handle_all_events()`
- [x] **Verbessertes INFO-Logging:** Start/End-Meldungen für alle Job-Phasen
- [x] **Frigate Timeouts erhöht:** `check_frigate_reachable` 15s→120s, `fetch_event`/`fetch_all_events` 30s→120s
- [x] **Download-Timeout tuple:** `(60, 600)` statt fix 300s — erlaubt Streaming ohne Gesamt-Timeout
- [x] **Frigate nginx-Timeout-Doku:** README beschreibt `proxy_read_timeout` auf 600s erhöhen
- [x] **Dynamische Retry-Limits pro Event-Dauer:** >3h = 3 Retries, 1-3h = 10, <1h = 30 (MAX_RETRY_ATTEMPTS)
- [x] **MAX_RETRY_ATTEMPTS default 50→30** (~5h statt ~8h Wartezeit)
- [x] **Mattermost-Benachrichtigung bei Aufgabe:** Event-Details, Kamera, Label, direkte Clip/Snapshot-URLs
- [x] **Download-Progress-Logging:** INFO-Level alle 50MB für Diagnose von Freeze-Punkten
- [x] **ChunkedEncodingError-Diagnose:** README-Doku für "korrupte Frigate-Segmente" hinzugefügt
- [x] **MQTT keepalive 60s→180s:** Schnellfix gegen "Keep alive timeout"-Disconnects während langer Downloads (richtige Lösung = Threading, siehe Punkt 6 oben)
- [x] **"Alles-OK"-Stille-Modus für Daily Health Report:** Neue Env-Variable `HEALTH_REPORT_ONLY_ON_ISSUES` (default `false`). Bei `true` werden OK-Reports nicht mehr an Mattermost geschickt, sondern nur als INFO-Log mit Kennzahlen festgehalten. WARNING und CRITICAL werden immer gesendet. Generischer `parse_bool_env()`-Helper akzeptiert true/false, yes/no, 1/0, on/off.
- [x] **Konfigurierbare Uhrzeit für Daily Health Report:** Neue Env-Variable `HEALTH_REPORT_TIME` (Format `HH:MM`, Default `09:00`, Container-TZ). Invalide Werte fallen mit WARNING-Log auf `09:00` zurück.
- [x] **Internet-Check 1× pro Job-Lauf statt pro Event:** `handle_not_uploaded_events()` und `handle_all_events()` rufen `internet()` einmal am Job-Anfang. `handle_single_event` akzeptiert `online`-Parameter (tri-state) und liefert `bool` zurück. Bei einem Upload-Fehler im Loop wird `internet()` neu geprüft und der Loop sauber abgebrochen, falls Konnektivität mittendrin verloren geht. Spart bei Internet-Outage bis zu 20 min an DNS-Timeouts pro 400-Event-Backlog.
- [x] **Partial Indexes für Retry-Queue:** `idx_pending_retry` und `idx_pending_hard` (Migration 3) — `select_not_uploaded_yet[_hard]()` ohne Full-Table-Scan, ORDER BY `created` direkt aus dem Index
- [x] **Download-Logs mit event_id:** Alle Progress/Complete/Abort-Messages enthalten jetzt die Event-ID für bessere Traceability bei parallelen Downloads
