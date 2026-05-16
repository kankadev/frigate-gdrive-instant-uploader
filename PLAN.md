# PLAN – Verbesserungsideen

Sammlung von Verbesserungen, die das Tool stabiler, schneller oder
ressourcen-schonender machen würden. Reihenfolge nach geschätztem Nutzen.

## 1. Datenbank-Index für die Retry-Queue

**Status:** offen
**Priorität:** mittel

`select_not_uploaded_yet()` filtert auf `uploaded`, `retry` und `created`.
Bei vielen Tausend Events (z.B. nach längerem Downtime) wird das ein
Full-Table-Scan alle 10 Minuten.

**Vorschlag:** Composite Index als Migration anlegen.

```sql
CREATE INDEX IF NOT EXISTS idx_pending
    ON events (uploaded, retry, created);
```

**Aufwand:** sehr klein (eine Migration in `database.run_migrations()`).

**Nutzen:** schnellerer Retry-Job bei großen DBs, weniger I/O.

---

## 2. Cache für `internet()`-Check

**Status:** offen
**Priorität:** niedrig

`internet()` (in `main.py`) macht für **jedes** Event einen DNS-Lookup auf
`8.8.8.8:53`. Beim 10-Min-Job mit z.B. 400 Backlog-Events sind das 400
zusätzliche Netzwerk-Calls.

**Vorschlag:**
- Ergebnis 30 Sekunden cachen (in-memory, mit Timestamp).
- Oder: einmal pro Job-Lauf prüfen und Ergebnis in `handle_not_uploaded_events`
  durchreichen.

**Aufwand:** klein.

**Nutzen:** weniger Netzwerkrauschen, leicht schnellerer Retry-Durchlauf,
robuster falls Internet-Check selbst kurz hängt.

---

## 3. Konfigurierbare Uhrzeit für Daily Health Report

**Status:** offen
**Priorität:** niedrig

Aktuell fest auf 09:00 Container-Zeit gepinnt
(`@/main.py: scheduler.add_job(daily_health_report, 'cron', hour=9, minute=0)`).

**Vorschlag:** Env-Variable `HEALTH_REPORT_TIME=09:00` mit Default 09:00,
geparst zu `hour`/`minute`.

---

## 4. Optionale "Alles OK"-Stille-Modus

**Status:** offen
**Priorität:** niedrig

Wenn alles in Ordnung ist, wird trotzdem täglich eine grüne Nachricht
verschickt. Manche User wollen nur Warnungen sehen.

**Vorschlag:** Env-Variable `HEALTH_REPORT_ONLY_ON_ISSUES=false` (default
false → wie bisher). Wenn `true`: OK-Reports werden nur als Debug geloggt,
nicht an Mattermost geschickt.

---

## 5. Frigate-Reachability-Check vor dem Retry-Job

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

## 6. Strukturierte Fehlerstatistiken in der DB

**Status:** offen
**Priorität:** niedrig

Aktuell wissen wir nur `tries` und `retry`. Wir wissen aber nicht, an welchem
Schritt es jeweils gescheitert ist (Download? Upload? Auth?).

**Vorschlag:** Spalte `last_error_kind` (z.B. `clip_400`, `frigate_5xx`,
`drive_quota`, `network`). Hilft beim Daily-Report, gezielter zu warnen.

**Aufwand:** mittel (Migration + Code-Anpassungen an mehreren Stellen).

---

## 7. Maximale Event-Dauer für Uploads

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
