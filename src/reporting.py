"""Read-only operational reporting; never download media or mutate queue state."""
import sqlite3
import time

import requests
from googleapiclient.errors import HttpError
from src.segmented_upload import MediaError
from src.frigate_api import FrigateUnreachableError


def classify_error(error):
    if isinstance(error, MediaError):
        return error.kind, error.detail, error.kind != 'media_transport_truncated'
    if isinstance(error, FrigateUnreachableError):
        return 'source_unreachable', 'Source request failed', False
    if isinstance(error, HttpError):
        status = int(error.resp.status)
        import json
        try:
            reasons = {e.get('reason') for e in json.loads(error.content).get('error', {}).get('errors', [])}
        except (ValueError, TypeError, AttributeError):
            reasons = set()
        limited = bool(reasons & {'rateLimitExceeded', 'userRateLimitExceeded', 'dailyLimitExceeded'})
        return 'drive_http_' + str(status), 'Drive returned HTTP ' + str(status), status in (400, 401, 403) and not limited
    if isinstance(error, requests.Timeout):
        return 'source_timeout', 'Source request timed out', False
    if isinstance(error, requests.ConnectionError):
        return 'source_connection', 'Source connection failed', False
    if isinstance(error, requests.HTTPError):
        status = error.response.status_code if error.response is not None else 0
        return 'source_http_' + str(status), 'Source returned HTTP ' + str(status), status in (400, 401, 403)
    if isinstance(error, requests.RequestException):
        return 'source_transport', 'Source transfer failed', False
    if isinstance(error, OSError):
        return 'io_blocked', 'I/O or spool capacity needs attention', False
    # Do not store arbitrary exception strings: they may contain credentials.
    return 'processing_' + type(error).__name__, 'Processing failed (' + type(error).__name__ + ')', True


def collect(db_path, now=None):
    now = time.time() if now is None else now
    c = sqlite3.connect('file:' + str(db_path) + '?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute('BEGIN')
        def scalar(sql, args=()):
            return c.execute(sql, args).fetchone()[0] or 0
        s = {
            'uploaded_24h': scalar('SELECT count(*) FROM upload_jobs WHERE state="complete" AND completed_at>=?', (now-86400,)),
            'active': scalar('SELECT count(*) FROM upload_jobs WHERE state="pending"'),
            'review': scalar('SELECT count(*) FROM upload_jobs WHERE state="review"'),
            'historical': scalar('SELECT count(*) FROM upload_jobs WHERE state IN ("partial","unavailable")'),
            'historical_with_parts': scalar('SELECT count(*) FROM upload_jobs j WHERE state IN ("partial","unavailable") AND EXISTS (SELECT 1 FROM upload_parts p WHERE p.event_id=j.event_id AND p.uploaded=1)'),
            'historical_parts': scalar('SELECT count(*) FROM upload_parts p JOIN upload_jobs j USING(event_id) WHERE j.state IN ("partial","unavailable") AND p.uploaded=1'),
            'gap_checks': scalar('SELECT count(DISTINCT p.event_id) FROM upload_parts p JOIN upload_jobs j USING(event_id) WHERE j.state="pending" AND p.coverage IN ("partial_pending","missing_pending")'),
            'errors_24h': scalar('SELECT count(*) FROM upload_jobs WHERE last_failure_at>=?', (now-86400,)),
            'new_gaps_24h': scalar('SELECT count(*) FROM upload_jobs WHERE state IN ("partial","unavailable") AND completed_at>=?', (now-86400,)),
            'oldest_active_days': scalar('SELECT max((?-e.start_time)/86400.0) FROM upload_jobs j JOIN events e USING(event_id) WHERE j.state IN ("pending","review")', (now,)),
            'last_success': scalar('SELECT max(completed_at) FROM upload_jobs WHERE state="complete"'),
        }
        s['active_errors'] = [tuple(r) for r in c.execute('SELECT error,count(*) FROM upload_jobs WHERE state IN ("pending","review") AND error IS NOT NULL GROUP BY error ORDER BY count(*) DESC')]
        s['open_parts'] = scalar('SELECT count(*) FROM upload_parts p JOIN upload_jobs j USING(event_id) WHERE j.state IN ("pending","review") AND p.uploaded=0 AND p.coverage!="unavailable"')
        return s
    finally:
        c.rollback()
        c.close()


def render(s, subsystems):
    broken = [name for name, ok in subsystems.items() if not ok]
    # Old confirmed gaps never cause a permanent current outage alarm.
    critical = bool(broken) or (s['active'] > s['gap_checks'] and s['uploaded_24h'] == 0)
    warning = bool(s['review'] or s['active_errors'] or s['new_gaps_24h'])
    ok = not critical and not warning
    level = 'KRITISCH' if critical else ('Handlungsbedarf' if warning else 'Betrieb aktiv')
    title = 'Frigate Uploader – ' + level
    color = '#d50000' if critical else ('#ffae42' if warning else '#36a64f')
    text = (
        '**Aktueller Betrieb**\n\n'
        f'- Vollständig archivierte Ereignisse in 24 Stunden: **{s["uploaded_24h"]}**\n'
        f'- In automatischer Bearbeitung: **{s["active"]}** '
        f'(davon **{s["gap_checks"]}** in Quelllücken-Bestätigung)\n'
        f'- Noch nicht archivierte Teile aktiver Ereignisse: **{s["open_parts"]}**\n'
        f'- Prüfung/Reparatur erforderlich: **{s["review"]}**\n'
        f'- Alter des ältesten aktiven/pruefbedürftigen Ereignisses: **{s["oldest_active_days"]:.1f} Tage**\n'
        f'- Ereignisse mit mindestens einem erfassten Fehler in 24 Stunden: **{s["errors_24h"]}** '
        '(einschließlich inzwischen behobener Fehler; keine Versuchsanzahl)\n'
    )
    if s['last_success']:
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(s['last_success'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        text += f'- Letztes vollständig archiviertes Ereignis: **{stamp}**\n'
    text += '\n**Aktuelle Fehlerursachen**\n\n'
    if s['active_errors']:
        for kind, count in s['active_errors']:
            text += f'- `{kind}`: **{count} Ereignisse**\n'
    else:
        text += '- Keine offenen Fehlerursachen erfasst.\n'
    text += (
        '\nAutomatische Bearbeitung bedeutet nicht, dass ein Export bereits geprüft ist. '
        'Nach drei gleichen deterministischen Fehlern ist eine gezielte Prüfung nötig; '
        'vorübergehende Verbindungsfehler werden weiter mit Abstand versucht.\n'
        '\n**Historische Quelllücken – getrennt vom laufenden Rückstand**\n\n'
        f'- Abgeschlossen mit fehlenden Quellabschnitten: **{s["historical"]} Ereignisse**\n'
        f'- Davon teilweise archiviert: **{s["historical_with_parts"]} Ereignisse**, '
        f'**{s["historical_parts"]} gespeicherte Teile**\n'
        f'- In den letzten 24 Stunden neu abgeschlossen mit Lücken: **{s["new_gaps_24h"]}**\n'
        '- Historische Lücken werden nicht als laufende Wiederholungen gezählt. '
        'Gesicherte Restaufnahmen bleiben erhalten; teilweise archiviert ist nicht vollständig.\n'
        '\n**Dienststatus**\n\n'
    )
    for name, healthy in subsystems.items():
        text += f'- {name}: {"OK" if healthy else "PRÜFEN"}\n'
    return title, text, color, ok
