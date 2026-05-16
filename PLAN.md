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

## Done

- [x] Hard-Fail-Cleanup: 404 auf `/api/events/{id}` → Auto-Delete
- [x] Clip-Cleanup: 404 auf `/clip.mp4` → Auto-Delete
- [x] Konservativer 400-Umgang: kein Auto-Delete (Datenintegrität)
- [x] Stale-Pending-Cleanup nach `STALE_PENDING_DAYS` (default 30)
- [x] Aufgeben nach `MAX_RETRY_ATTEMPTS` Versuchen (default 50)
- [x] Aufnahme-Timestamp in Log-Messages für bessere Lesbarkeit
- [x] Daily Health Report (grün/orange/rot) via Mattermost
- [x] Upload-Lock gegen SSL-Race-Conditions
- [x] SQLite WAL-Mode für Concurrency
- [x] Python 3.12 + gepinnte Dependencies
