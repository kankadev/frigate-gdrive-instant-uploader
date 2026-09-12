"""Durable work queue. MQTT callbacks only enqueue; HTTP also reconciles events."""
import json
import sqlite3
import time
from contextlib import contextmanager
from src import database


@contextmanager
def connection():
    conn = sqlite3.connect(database.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    with connection() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS upload_jobs (
            event_id TEXT PRIMARY KEY, metadata TEXT, next_attempt REAL DEFAULT 0,
            failures INTEGER DEFAULT 0, missing_since REAL,
            state TEXT DEFAULT 'pending', error TEXT, completed_at REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS upload_parts (
            event_id TEXT, part INTEGER, start REAL, end REAL, duration REAL,
            drive_id TEXT, size INTEGER, md5 TEXT, uploaded INTEGER DEFAULT 0,
            PRIMARY KEY(event_id, part))''')
        # Import legacy failures once. A retry budget must never erase a video.
        c.execute('''INSERT OR IGNORE INTO upload_jobs(event_id)
                     SELECT event_id FROM events WHERE uploaded=0''')
        c.execute('''UPDATE events SET retry=1 WHERE uploaded=0 AND event_id IN
                     (SELECT event_id FROM upload_jobs WHERE state='pending') AND retry=0''')


def enqueue(event):
    if not event.get('end_time') or not event.get('has_clip'):
        return
    with connection() as c:
        c.execute('INSERT OR IGNORE INTO events(event_id,start_time) VALUES (?,?)',
                  (event['id'], event['start_time']))
        uploaded = c.execute('SELECT uploaded FROM events WHERE event_id=?', (event['id'],)).fetchone()[0]
        if uploaded:
            return
        # Updating metadata must not reset backoff or a confirmed missing result.
        c.execute('''INSERT INTO upload_jobs(event_id,metadata,next_attempt) VALUES (?,?,?)
                     ON CONFLICT(event_id) DO UPDATE SET metadata=excluded.metadata''',
                  (event['id'], json.dumps(event), event['end_time'] + 30))


def due():
    with connection() as c:
        return [dict(r) for r in c.execute('''SELECT j.* FROM upload_jobs j JOIN events e
            USING(event_id) WHERE j.state='pending' AND e.uploaded=0 AND next_attempt<=?
            ORDER BY next_attempt,event_id LIMIT 100''', (time.time(),))]


def failed(event_id, kind, missing=False):
    now = time.time()
    with connection() as c:
        row = c.execute('SELECT * FROM upload_jobs WHERE event_id=?', (event_id,)).fetchone()
        first = row['missing_since'] if missing else None
        if missing and first is None:
            first = now
        # Require separate successful source checks at least an hour apart.
        terminal = missing and now-first >= 3600
        count = row['failures'] + 1
        delay = min(3600, 60 * 2 ** min(count-1, 6))
        c.execute('''UPDATE upload_jobs SET failures=?,next_attempt=?,missing_since=?,
                     state=?,error=? WHERE event_id=?''',
                  (count, now+delay, first, 'unavailable' if terminal else 'pending', kind, event_id))
        c.execute('''UPDATE events SET uploaded=0,retry=?,tries=tries+1,last_error_kind=?
                     WHERE event_id=?''', (0 if terminal else 1, kind, event_id))


def defer(event_id, seconds=5):
    with connection() as c:
        c.execute('UPDATE upload_jobs SET next_attempt=?,missing_since=NULL WHERE event_id=?',
                  (time.time()+seconds, event_id))


def complete(event_id):
    with connection() as c:
        c.execute("UPDATE upload_jobs SET state='complete',completed_at=?,error=NULL WHERE event_id=?",
                  (time.time(), event_id))
        c.execute('UPDATE events SET uploaded=1,retry=1,last_error_kind=NULL WHERE event_id=?', (event_id,))


def parts(event_id):
    with connection() as c:
        return [dict(r) for r in c.execute('SELECT * FROM upload_parts WHERE event_id=? ORDER BY part', (event_id,))]


def save_plan(event_id, plan):
    with connection() as c:
        for i, p in enumerate(plan):
            c.execute('''INSERT OR IGNORE INTO upload_parts(event_id,part,start,end,duration)
                         VALUES (?,?,?,?,?)''', (event_id, i, p['start'], p['end'], p['duration']))


def update_part(event_id, part, **values):
    assert set(values) <= {'drive_id', 'size', 'md5', 'uploaded'}
    with connection() as c:
        c.execute('UPDATE upload_parts SET '+','.join(k+'=?' for k in values)+' WHERE event_id=? AND part=?',
                  (*values.values(), event_id, part))
