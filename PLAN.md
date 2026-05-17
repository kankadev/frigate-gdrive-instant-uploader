# PLAN – Verbesserungsideen

Sammlung von Verbesserungen, die das Tool stabiler, schneller oder
ressourcen-schonender machen würden. Reihenfolge nach geschätztem Nutzen.

## 1. Maximale Event-Dauer für Uploads

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

## 2. `fetch_all_events` als Generator (Streaming)

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

## 3. MQTT `on_message` in separaten Thread auslagern

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

**Abhängigkeit:** Punkt 4 (Threading / Parallel-Uploads) — die gleichen
SQLite-Concurrency-Probleme gelten hier. WAL-Mode hilft, aber Connection-Sharing
zwischen Threads kann trotzdem zu `database is locked` führen.

**Aufwand:** mittel (Threading + DB-Connection-Handling).
**Nutzen:** Keine MQTT-Disconnects mehr, Events werden sofort empfangen statt
verspätet.

---

## 4. Threading / Parallel-Uploads (mit SQLite-Warnung)

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
- [x] **MQTT keepalive 60s→180s:** Schnellfix gegen "Keep alive timeout"-Disconnects während langer Downloads (richtige Lösung = Threading, siehe Punkt 3 oben)
- [x] **`MAX_CLIP_SIZE` mit Pre-flight HEAD-Check und Streaming-Abort:** Env-Variable `MAX_CLIP_SIZE` (human-readable: `5GB`, `500MB`, `0`/leer = off). `_parse_max_clip_size()` mit Regex. Vor Download: HEAD-Request auf die Clip-URL, bei `Content-Length > Limit` sofort `ClipTooLargeError`. Falls HEAD nicht unterstützt: während des Streamings zählen und bei Überschreitung abbrechen. Event wird via `update_event_retry(0, last_error_kind=ERR_CLIP_TOO_LARGE)` als non-retriable markiert, Metadaten bleiben in der DB erhalten.
- [x] **Strukturierte Fehlerstatistiken in der DB:** Neue Spalte `last_error_kind` (Migration 4) mit coarse-grained Kategorien (`frigate_download_timeout`, `frigate_download_5xx`, `frigate_download_truncated`, `frigate_download_empty`, `clip_too_large`, `drive_5xx`, `drive_http`, `drive_network`, `drive_other`). `upload_to_google_drive()` und `download_video_with_retry()` liefern jetzt `(result, error_kind)`-Tuples. `database.update_event()` / `update_event_retry()` akzeptieren `last_error_kind`. Bei Erfolg wird die Spalte auf NULL zurückgesetzt. Daily Health Report zeigt Aufschlüsselung der wartenden Events nach Fehler-Kategorie.
- [x] **Edge-triggered Mattermost-Notifications bei Frigate-Outage:** State-Variable `_frigate_unreachable_since` + idempotente Helper `_notify_frigate_unreachable_once()` / `_notify_frigate_recovered_once()`. Maximal 2 Notifications pro Outage-Zyklus (Down + Recovery mit Downtime-Dauer), kein Spam bei längeren Ausfällen. Bewusst nicht für Internet-Outage (kein Webhook erreichbar). Container-Restart während Outage führt maximal zu 1 Duplikat-Notification.
- [x] **Frigate-Reachability-Pre-Check pro Job-Lauf:** `handle_not_uploaded_events()` und `handle_all_events()` prüfen am Job-Anfang per `check_frigate_reachable()`, ob Frigate erreichbar ist. Default-Timeout der Funktion von 120s auf 10s reduziert (`/api/version` antwortet in Millisekunden, langer Timeout war historisch falsch). Bei Frigate-Outage wird der gesamte Job in 10s übersprungen statt bis zu 6 min Reachability-Timeouts zu durchlaufen. Der bestehende Per-Event-Check im Retry-Loop bleibt als Race-Schutz erhalten und profitiert ebenfalls vom kürzeren Timeout.
- [x] **"Alles-OK"-Stille-Modus für Daily Health Report:** Neue Env-Variable `HEALTH_REPORT_ONLY_ON_ISSUES` (default `false`). Bei `true` werden OK-Reports nicht mehr an Mattermost geschickt, sondern nur als INFO-Log mit Kennzahlen festgehalten. WARNING und CRITICAL werden immer gesendet. Generischer `parse_bool_env()`-Helper akzeptiert true/false, yes/no, 1/0, on/off.
- [x] **Konfigurierbare Uhrzeit für Daily Health Report:** Neue Env-Variable `HEALTH_REPORT_TIME` (Format `HH:MM`, Default `09:00`, Container-TZ). Invalide Werte fallen mit WARNING-Log auf `09:00` zurück.
- [x] **Internet-Check 1× pro Job-Lauf statt pro Event:** `handle_not_uploaded_events()` und `handle_all_events()` rufen `internet()` einmal am Job-Anfang. `handle_single_event` akzeptiert `online`-Parameter (tri-state) und liefert `bool` zurück. Bei einem Upload-Fehler im Loop wird `internet()` neu geprüft und der Loop sauber abgebrochen, falls Konnektivität mittendrin verloren geht. Spart bei Internet-Outage bis zu 20 min an DNS-Timeouts pro 400-Event-Backlog.
- [x] **Partial Indexes für Retry-Queue:** `idx_pending_retry` und `idx_pending_hard` (Migration 3) — `select_not_uploaded_yet[_hard]()` ohne Full-Table-Scan, ORDER BY `created` direkt aus dem Index
- [x] **Download-Logs mit event_id:** Alle Progress/Complete/Abort-Messages enthalten jetzt die Event-ID für bessere Traceability bei parallelen Downloads
