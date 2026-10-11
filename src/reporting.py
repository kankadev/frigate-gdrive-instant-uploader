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
    level = 'CRITICAL' if critical else ('Action required' if warning else 'Operational')
    title = 'Frigate Uploader – ' + level
    color = '#d50000' if critical else ('#ffae42' if warning else '#36a64f')
    text = (
        '**Current operations**\n\n'
        f'- Events fully archived in the last 24 hours: **{s["uploaded_24h"]}**\n'
        f'- Events being processed automatically: **{s["active"]}** '
        f'(including **{s["gap_checks"]}** awaiting source-gap confirmation)\n'
        f'- Unarchived parts of active events: **{s["open_parts"]}**\n'
        f'- Events requiring review or repair: **{s["review"]}**\n'
        f'- Age of oldest active or review-required event: **{s["oldest_active_days"]:.1f} days**\n'
        f'- Events with at least one recorded failure in the last 24 hours: **{s["errors_24h"]}** '
        '(includes resolved failures; not a retry-attempt count)\n'
    )
    if s['last_success']:
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(s['last_success'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        text += f'- Last fully archived event: **{stamp}**\n'
    text += '\n**Current failure categories**\n\n'
    if s['active_errors']:
        for kind, count in s['active_errors']:
            text += f'- `{kind}`: **{count} events**\n'
    else:
        text += '- No unresolved failures recorded.\n'
    text += (
        '\nAutomatic processing does not mean an export has already been verified. '
        'Three consecutive identical deterministic failures require targeted review; '
        'transient connection failures continue to retry with backoff.\n'
        '\n**Historical source gaps – separate from the active backlog**\n\n'
        f'- Closed with missing source intervals: **{s["historical"]} events**\n'
        f'- Of these, partially archived: **{s["historical_with_parts"]} events**, '
        f'**{s["historical_parts"]} archived parts**\n'
        f'- Newly closed with gaps in the last 24 hours: **{s["new_gaps_24h"]}**\n'
        '- Historical gaps are not counted as active retries. '
        'Rescued recordings are preserved; partially archived does not mean complete.\n'
        '\n**Subsystem status**\n\n'
    )
    for name, healthy in subsystems.items():
        text += f'- {name}: {"OK" if healthy else "CHECK"}\n'
    return title, text, color, ok
