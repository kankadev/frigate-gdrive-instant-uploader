# PLAN – Verbesserungsideen

Sammlung von Verbesserungen, die das Tool stabiler, schneller oder
ressourcen-schonender machen würden. Reihenfolge nach geplanter
Umsetzungsreihenfolge (oben = als nächstes dran).

## 1. MQTT `on_message` in separaten Thread auslagern

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

**Abhängigkeit:** Punkt 10 (Threading / Parallel-Uploads) — die gleichen
SQLite-Concurrency-Probleme gelten hier. WAL-Mode hilft, aber Connection-Sharing
zwischen Threads kann trotzdem zu `database is locked` führen.

**Aufwand:** mittel (Threading + DB-Connection-Handling).
**Nutzen:** Keine MQTT-Disconnects mehr, Events werden sofort empfangen statt
verspätet.

---

## 2. Drive-Folder-Cache invalidieren bei 404

**Status:** offen
**Priorität:** mittel-hoch

`_folder_id_cache` in `src/google_drive.py` wird befüllt, aber **nie**
invalidiert. Wenn der User manuell einen Ordner in Google Drive löscht (z.B.
zum Aufräumen), nutzt das Tool für die ganze Container-Laufzeit weiterhin die
gecachte Folder-ID → Drive antwortet `404` auf `files().create(parents=[...])`
→ alle Uploads in diesen Ordner schlagen permanent fehl, bis der Container
neu gestartet wird.

**Vorschlag:** Im `HttpError`-Handler von `upload_to_google_drive` bei
`status==404` mit "parent not found"-Body den entsprechenden Cache-Eintrag
invalidieren und einmal neu auflösen, dann Retry. Alternativ: TTL auf den
Cache (z.B. 1h) — einfacher, aber mit kleinem Performance-Tradeoff.

**Aufwand:** klein (~20 LOC).
**Nutzen:** Mittel — passiert real bei jedem User, der irgendwann mal in
Drive aufräumt. Verhindert die Klasse "Container läuft, aber alle Uploads
schlagen fehl bis Neustart".

---

## 3. Daily Health Report erweitert, 6-stündiger Job entfernt, Clip-Availability-Statistik

**Status:** erledigt
**Priorität:** mittel

Der 6-stündige Job (`run_every_6_hours`) wurde entfernt, da er redundant ist:
- Pro-Event Notifications liefern bereits Details zu JEDEM failed Event (Kamera, Label, URLs, Fehlergrund)
- Daily Health Report liefert die tägliche Statistik mit Error-Kinds und System-Zustand
- Die 6-stündige ID-Liste ohne Kontext bringt keinen Mehrwert

Der Daily Health Report wurde mit den folgenden Metriken erweitert:
- **Subsystem-Status:** DB, Scheduler, MQTT (mit Icons :white_check_mark:/:x:)
- **Uptime:** Laufzeit des Containers seit Start (z.B. "2d 14h 30m")
- **Letzter erfolgreicher Upload:** Timestamp des letzten erfolgreichen Uploads oder "_never_"
- **DB-Größe:** Dateigröße der SQLite-DB in human-readable Format (KB/MB/GB)
- **Frigate-Reachability:** "reachable" oder "unreachable for Xd Yh Zm" (basierend auf edge-triggered Notifications)
- **Clip-Availability-Statistik:** Unterscheidet zwischen pending events mit Clips verfügbar auf Frigate (Handlungsbedarf) und Clips nicht mehr verfügbar (nichts zu tun). Für das älteste pending Event wird angezeigt, ob der Clip noch verfügbar ist.

Zusätzliche Änderungen:
- `PROGRAM_START_TIME` global für Uptime-Tracking
- `sqlite3` import in main.py für DB-Health-Check
- `database.get_last_successful_upload_timestamp()` Funktion hinzugefügt
- `_check_clip_availability(event_id)` Funktion (HEAD-Request auf Clip-URL)
- `_get_clip_availability_stats()` Funktion (prüft alle pending events)
- Helper-Funktionen für alle neuen Metriken
- `daily_health_report()` nimmt jetzt `scheduler` Parameter für Subsystem-Status

---

## 4. Daily Health Report mit Retry bei Mattermost-Failure

**Status:** offen
**Priorität:** mittel

`send_mattermost_notification` im `daily_health_report()` ist Single-Shot.
Wenn Mattermost um 09:00:00 für 30 Sekunden hängt, ist der ganze Tagesreport
weg — du erfährst es erst morgen 09:00. Genau dann, wenn der Report dir
sagen würde "es gab gestern Probleme", weißt du nichts.

**Vorschlag:** Retry mit Exponential Backoff direkt in
`send_mattermost_notification` — z.B. 3 Versuche, 30s/60s/120s. Bei
endgültigem Fehlschlag: WARNING-Log mit Hinweis "Mattermost unreachable;
report content was: ...".

Optional: bei kritischen Reports (CRITICAL-Status) den Inhalt zusätzlich
in eine lokale Datei `logs/missed_reports.log` schreiben, damit nichts
verloren geht.

**Aufwand:** klein (~15 LOC).
**Nutzen:** Mittel — Daily Report ist die primäre Statusquelle. Wenn er
silently versagt, weißt du nicht, dass du nichts weißt.

---

## 4. Graceful Shutdown bei SIGTERM/SIGINT

**Status:** offen
**Priorität:** mittel

`main()` fängt nur `KeyboardInterrupt`/`SystemExit` ab und ruft
`scheduler.shutdown()`. Der MQTT-Thread ist `daemon=True` und stirbt abrupt.
Laufende Uploads in `upload_to_google_drive` werden mid-stream abgebrochen.
Konsequenzen:

- Resumable-Upload-Sessions bleiben verwaist auf Drive (Drive räumt sie nach
  7 Tagen auf, aber bis dahin tauchen sie in Quota-Berechnungen auf).
- Halb-geladene Files können auf Drive bleiben.
- Der DB-Status ist u.U. inkonsistent (Event als pending markiert, aber
  Upload war zu 80% durch).

**Vorschlag:**
- Signal-Handler für `SIGTERM` (Docker-Stop) und `SIGINT` (Ctrl+C).
- Globales `shutdown_event = threading.Event()`.
- `handle_*_events`-Loops prüfen `shutdown_event.is_set()` zwischen Events.
- Aktuell laufender Upload darf zu Ende gehen mit Timeout (z.B. 60s).
- MQTT-Client `disconnect()` sauber.
- Scheduler `shutdown(wait=True)`.
- Anschließend `sys.exit(0)`.

**Aufwand:** mittel (~40 LOC + sorgfältiges Threading-Handling).
**Nutzen:** Mittel — vor allem für Docker-Restarts / Container-Updates.
Verhindert Orphan-Uploads und inkonsistente DB-States.

---

## 5. Tests für Parser, DB-Layer und Helpers

**Status:** offen
**Priorität:** mittel (langfristig hoch)

Bei aktuell ~700 LOC in `main.py` und vielen Edge-Cases (Race-Conditions,
Internet/Frigate-Outage-Pfade, Migration-Idempotenz, SQL-Building mit
optionalen Parametern) gibt es kein einziges Test-File. Jedes Refactoring
ist ein Vertrauensschritt ins Ungewisse.

**Vorschlag:** `pytest` als Dev-Dependency aufnehmen, Tests/Ordner anlegen,
und mit den einfachen, gut isolierbaren Stellen anfangen:

- **Parser-Helper** (rein, deterministisch, kein I/O):
  - `parse_bool_env()` — alle akzeptierten Truthy/Falsy-Varianten + Defaults.
  - `parse_health_report_time()` — gültige + ungültige Werte, Fallback.
  - `_parse_max_clip_size()` — `5GB`/`500MB`/`0`/`""`/Bogus.
  - `_parse_skip_events_longer_than()` — Werte/Negativ/Bogus.
  - `_format_duration()` — Sekunden/Minuten/Stunden-Grenzen.
- **DB-Layer mit `:memory:`-SQLite-Connection:**
  - `update_event()`-SQL-Building für alle Parameter-Kombinationen.
  - `update_event_retry()` mit/ohne `last_error_kind`.
  - `get_health_stats()` mit gefixtetem DB-State.
  - Migration-Idempotenz (Run twice → kein Crash).
- **Logik-Helper:**
  - `get_max_retries_for_event()` Dauer-Buckets.
  - `_notify_frigate_unreachable_once()` / `_notify_frigate_recovered_once()`
    Edge-Triggering (mit Mock-Mattermost).

**Bewusst nicht im ersten Wurf:** echte Frigate/Drive-Integration-Tests —
die brauchen Mocking-Infrastruktur und sind High-Effort/Low-Yield.

**Aufwand:** mittel (~1 Tag für ~30 sinnvolle Tests).
**Nutzen:** Hoch (langfristig) — schützt vor Regressionen bei jeder
zukünftigen Änderung. Macht Refactoring entspannter.

---

## 6. Drive-Service Lazy-Init mit Auto-Reauth

**Status:** offen
**Priorität:** niedrig–mittel
**Risiko:** **größerer Architektur-Eingriff**

`service = get_google_service()` läuft beim **Modul-Import** in
`src/google_drive.py:117`. Konsequenzen:

1. Wenn Google bei Container-Start für 30 Sekunden hängt, crasht der
   Container sofort. Mit `restart: unless-stopped` → Restart-Loop.
2. Das Service-Objekt wird **nie** neu erzeugt. Bei Token-Revoke,
   Schlüsselrotation, oder Auth-Refresh-Bugs bricht der gesamte Upload-Pfad
   weg, bis du den Container manuell neu startest.

**Vorschlag:**
- `service` aus dem Modul-Scope entfernen.
- Privater Getter `_get_service()` mit Lazy-Init und Caching.
- Bei `HttpError` mit Status `401`/`403` Service-Cache invalidieren und
  einmal neu auth'en (innerhalb des bestehenden Retry-Loops in
  `upload_to_google_drive`).
- Initial-Auth beim Container-Start mit Retry (z.B. 3 × 30s Backoff)
  bevor man aufgibt.

**WICHTIG / Risiko:** Berührt jeden Aufruf von `service.files().*` (mehrere
Dutzend Stellen). Folder-Cache, Cleanup-Job, Upload — alles muss umgestellt
werden. Hohes Regressions-Risiko ohne Tests (siehe Punkt 5).

**Aufwand:** mittel-hoch.
**Nutzen:** Hoch — verhindert die häufigste "Warum läuft das nicht?"-
Ursache, die im Langzeitbetrieb auftritt.

**Empfehlung:** Erst nach Punkt 5 (Tests) angehen, damit man Regressionen
bemerkt.

---

## 7. Memory-Streaming statt Bytes-Buffer

**Status:** offen
**Priorität:** niedrig–mittel
**Risiko:** **größerer Architektur-Eingriff**

In `download_video_with_retry()` wird der gesamte Clip in einer Bytes-
Variable gehalten:

```python
fh = tempfile.TemporaryFile()
# ...write chunks...
fh.seek(0)
data = fh.read()  # ← KOMPLETTER Clip jetzt im RAM
return data, None
```

Anschließend:
```python
media = MediaIoBaseUpload(io.BytesIO(video_data), ...)  # ← ZWEITE Kopie im RAM
```

Bei einem 4 GB-Clip = bis zu **8 GB Python-RAM** während des Uploads. Auf
LXC-Containern mit 1–2 GB RAM ist das ein OOM-Kill-Garant. Mit
`MAX_CLIP_SIZE=5GB` potenziell sogar 10 GB Spike.

**Vorschlag:** `download_video_with_retry()` gibt statt `bytes` ein
File-Handle (oder Pfad zur Temp-Datei) zurück, das direkt an
`MediaIoBaseUpload(fh, resumable=True, chunksize=...)` geht. Die Drive-API
liest dann chunked von der Disk → RAM-Footprint bleibt bei ~10 MB.

**WICHTIG / Risiko:** Berührt die komplette Download/Upload-Pipeline,
inkl. Error-Handling, Retry-Logik, Lifecycle des Temp-File-Handles.
Edge-Cases: was passiert mit dem Handle, wenn Upload fehlschlägt und
nochmal retry'd wird? Wer schließt das File? Memory-Map vs. Streaming?

**Aufwand:** mittel-hoch.
**Nutzen:** Sehr hoch — eliminiert OOM-Risiko bei großen Clips, der
einzige reale Show-Stopper im Memory-Bereich.

**Empfehlung:** Erst nach Punkt 5 (Tests) angehen.

---

## 8. `fetch_all_events` als Generator (Streaming)

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

## 9. Threading / Parallel-Uploads (mit SQLite-Warnung)

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
- [x] **Konfigurations-Validierung beim Start:** `validate_config()` in `main()` prüft alle Pflicht-Variablen (`FRIGATE_URL`, `MQTT_*`, `SERVICE_ACCOUNT_FILE`, `UPLOAD_DIR`) und validiert Ranges (`MQTT_PORT`, `MAX_RETRY_ATTEMPTS`). Sammelt ALLE Fehler vor dem Beenden, gibt pro Problem eine klare Fehlermeldung mit konkretem Fix-Hinweis (`CONFIG ERROR: FRIGATE_URL is not set. Please add...`). URL-Sanity-Check (`http://` oder `https://`). File-Existenz-Check für Service-Account. Maskiertes Config-Logging beim Start: alle aktiven Werte mit `***` für Secrets, damit man sofort sieht was geladen wurde. Fail-fast statt kryptischer Runtime-Crashes deep im Request-Stack.
- [x] **MQTT keepalive 60s→180s:** Schnellfix gegen "Keep alive timeout"-Disconnects während langer Downloads (richtige Lösung = Threading, siehe Punkt 1 oben)
- [x] **Healthcheck-HTTP-API mit `/health` und `/status`:** Stdlib-only `ThreadingHTTPServer` (keine neue Dependency), bind via `HEALTHCHECK_BIND` (default `0.0.0.0`) auf `HEALTHCHECK_PORT` (default `8080`). `/health` ist unauthenticated (Docker-`HEALTHCHECK`-Probe), liefert `200` wenn DB + Scheduler ok, `503` wenn nicht oder während Shutdown — MQTT-Disconnects flunken bewusst NICHT (periodischer Job ist das Sicherheitsnetz). `/status` liefert detailliertes JSON mit Stats und Error-Kind-Aufschlüsselung, optional via `HEALTHCHECK_TOKEN` mit Bearer-Token gesichert (constant-time-Vergleich via `hmac.compare_digest`). Sicherheitshärtung: Methoden-Whitelist (nur GET/HEAD), Security-Header (`X-Content-Type-Options`, `X-Frame-Options`, `Cache-Control: no-store`), Access-Logs unterdrückt, keine sensiblen Daten im Body (keine Pfade, URLs, Webhooks, Event-IDs). `Dockerfile` bekommt `HEALTHCHECK`-Direktive (probt `127.0.0.1` via stdlib `urllib`, kein curl-Dep). Verifikation: 14 isolierte Tests für Happy-Path, Subsystem-Failures, Auth-Wrong/Right/Open, HEAD/POST/404, Security-Header.
- [x] **`SKIP_EVENTS_LONGER_THAN_SECONDS` Dauer-Filter:** Neue Env-Variable (Default `0` = off, Wert in Sekunden). Greift in `handle_single_event` direkt nach dem DB-Insert und bevor irgendein Frigate-Clip-Roundtrip stattfindet — also auch im MQTT-Pfad ohne zusätzliche API-Calls. Komplementär zu `MAX_CLIP_SIZE`: fängt Events ab, die zwar klein, aber sehr lang sind (z.B. 5h Low-Bitrate-Crop, 1.5 GB). Event wird via `update_event_retry(0, last_error_kind=ERR_EVENT_TOO_LONG)` non-retriable markiert, Metadaten bleiben in der DB.
- [x] **`MAX_CLIP_SIZE` mit Pre-flight HEAD-Check und Streaming-Abort:** Env-Variable `MAX_CLIP_SIZE` (human-readable: `5GB`, `500MB`, `0`/leer = off). `_parse_max_clip_size()` mit Regex. Vor Download: HEAD-Request auf die Clip-URL, bei `Content-Length > Limit` sofort `ClipTooLargeError`. Falls HEAD nicht unterstützt: während des Streamings zählen und bei Überschreitung abbrechen. Event wird via `update_event_retry(0, last_error_kind=ERR_CLIP_TOO_LARGE)` als non-retriable markiert, Metadaten bleiben in der DB erhalten.
- [x] **Strukturierte Fehlerstatistiken in der DB:** Neue Spalte `last_error_kind` (Migration 4) mit coarse-grained Kategorien (`frigate_download_timeout`, `frigate_download_5xx`, `frigate_download_truncated`, `frigate_download_empty`, `clip_too_large`, `drive_5xx`, `drive_http`, `drive_network`, `drive_other`). `upload_to_google_drive()` und `download_video_with_retry()` liefern jetzt `(result, error_kind)`-Tuples. `database.update_event()` / `update_event_retry()` akzeptieren `last_error_kind`. Bei Erfolg wird die Spalte auf NULL zurückgesetzt. Daily Health Report zeigt Aufschlüsselung der wartenden Events nach Fehler-Kategorie.
- [x] **Edge-triggered Mattermost-Notifications bei Frigate-Outage:** State-Variable `_frigate_unreachable_since` + idempotente Helper `_notify_frigate_unreachable_once()` / `_notify_frigate_recovered_once()`. Maximal 2 Notifications pro Outage-Zyklus (Down + Recovery mit Downtime-Dauer), kein Spam bei längeren Ausfällen. Bewusst nicht für Internet-Outage (kein Webhook erreichbar). Container-Restart während Outage führt maximal zu 1 Duplikat-Notification.
- [x] **Frigate-Reachability-Pre-Check pro Job-Lauf:** `handle_not_uploaded_events()` und `handle_all_events()` prüfen am Job-Anfang per `check_frigate_reachable()`, ob Frigate erreichbar ist. Default-Timeout der Funktion von 120s auf 10s reduziert (`/api/version` antwortet in Millisekunden, langer Timeout war historisch falsch). Bei Frigate-Outage wird der gesamte Job in 10s übersprungen statt bis zu 6 min Reachability-Timeouts zu durchlaufen. Der bestehende Per-Event-Check im Retry-Loop bleibt als Race-Schutz erhalten und profitiert ebenfalls vom kürzeren Timeout.
- [x] **"Alles-OK"-Stille-Modus für Daily Health Report:** Neue Env-Variable `HEALTH_REPORT_ONLY_ON_ISSUES` (default `false`). Bei `true` werden OK-Reports nicht mehr an Mattermost geschickt, sondern nur als INFO-Log mit Kennzahlen festgehalten. WARNING und CRITICAL werden immer gesendet. Generischer `parse_bool_env()`-Helper akzeptiert true/false, yes/no, 1/0, on/off.
- [x] **Konfigurierbare Uhrzeit für Daily Health Report:** Neue Env-Variable `HEALTH_REPORT_TIME` (Format `HH:MM`, Default `09:00`, Container-TZ). Invalide Werte fallen mit WARNING-Log auf `09:00` zurück.
- [x] **Internet-Check 1× pro Job-Lauf statt pro Event:** `handle_not_uploaded_events()` und `handle_all_events()` rufen `internet()` einmal am Job-Anfang. `handle_single_event` akzeptiert `online`-Parameter (tri-state) und liefert `bool` zurück. Bei einem Upload-Fehler im Loop wird `internet()` neu geprüft und der Loop sauber abgebrochen, falls Konnektivität mittendrin verloren geht. Spart bei Internet-Outage bis zu 20 min an DNS-Timeouts pro 400-Event-Backlog.
- [x] **Partial Indexes für Retry-Queue:** `idx_pending_retry` und `idx_pending_hard` (Migration 3) — `select_not_uploaded_yet[_hard]()` ohne Full-Table-Scan, ORDER BY `created` direkt aus dem Index
- [x] **Download-Logs mit event_id:** Alle Progress/Complete/Abort-Messages enthalten jetzt die Event-ID für bessere Traceability bei parallelen Downloads
