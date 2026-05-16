import os
import sqlite3
import logging
from dotenv import load_dotenv

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db/events.db')
load_dotenv()

# Single retention period for the local SQLite DB.
# After DB_RETENTION_DAYS days an event is removed regardless of upload status:
#   - uploaded=1: Drive file remains; the DB row served only as a dedup marker.
#   - uploaded=0: Frigate's typical retention is 14 days, so after DB_RETENTION_DAYS
#     the clip is definitively gone and retrying is pointless.
# Backwards-compat: respect the legacy EVENT_RETENTION_DAYS / STALE_PENDING_DAYS
# variables if either is set; otherwise default to 30 days.
DB_RETENTION_DAYS = int(
    os.getenv(
        'DB_RETENTION_DAYS',
        os.getenv('STALE_PENDING_DAYS', os.getenv('EVENT_RETENTION_DAYS', '30'))
    )
)


def init_db(db_path=DB_PATH):
    logging.info(f"Initializing database at {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        # Enable WAL mode for safer concurrent access from multiple threads
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        # Create the events table if it does not exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, 
                start_time REAL,
                uploaded BOOLEAN DEFAULT 0,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tries INTEGER DEFAULT 0,
                retry BOOLEAN DEFAULT 1
            )
        ''')

        # Create the migrations table if it does not exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Trigger für last_updated hinzufügen
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS update_last_updated
            AFTER UPDATE ON events
            FOR EACH ROW
            BEGIN
                UPDATE events
                SET last_updated = CURRENT_TIMESTAMP
                WHERE event_id = OLD.event_id;
            END;
        ''')

        conn.commit()
    except Exception as e:
        logging.error(f"Error initializing database: {e}")
        conn.rollback()
    finally:
        conn.close()


def run_migrations(migrations_folder='db/migrations'):
    conn = sqlite3.connect(DB_PATH)

    try:
        cursor = conn.cursor()

        cursor.execute('SELECT name FROM migrations')
        applied_migrations = set(row[0] for row in cursor.fetchall())

        for filename in sorted(os.listdir(migrations_folder)):
            if filename.endswith('.py') and filename not in applied_migrations:
                migration_path = os.path.join(migrations_folder, filename)
                logging.info(f"Running migration: {migration_path}")
                try:
                    with open(migration_path) as file:
                        logging.debug(f"Executing migration {filename}")
                        exec(file.read(), globals())
                    cursor.execute('INSERT INTO migrations (name) VALUES (?)', (filename,))
                    conn.commit()
                    logging.info(f"Migration {filename} applied successfully.")
                except Exception as e:
                    logging.error(f"Error applying migration {filename}: {e}")
                    conn.rollback()
    except Exception as e:
        logging.error(f"Error running migrations: {e}")
    finally:
        conn.close()


def is_event_exists(event_id, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM events WHERE event_id = ?', (event_id,))
        result = cursor.fetchone()
        return result is not None
    except Exception as e:
        logging.error(f"Error checking event existence: {e}")
        return False
    finally:
        conn.close()


def insert_event(event_id, start_time, db_path=DB_PATH):
    """
    Inserts an event into the database.
    :param event_id:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO events (event_id, start_time) VALUES (?, ?)', (event_id, start_time))
        conn.commit()
    except Exception as e:
        logging.error(f"Error inserting event: {e}")
    finally:
        conn.close()


def update_event(event_id, uploaded, retry=None, db_path=DB_PATH):
    """
    Updates an event in the database.
    :param event_id:
    :param uploaded:
    :param retry:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    if retry is not None:
        cursor.execute('UPDATE events SET uploaded = ?, retry = ?, tries = tries + 1 WHERE event_id = ?',
                       (uploaded, retry, event_id))
    else:
        cursor.execute('UPDATE events SET uploaded = ?, tries = tries + 1 WHERE event_id = ?', (uploaded, event_id))
    conn.commit()
    conn.close()


def select_retry(event_id, db_path=DB_PATH):
    """
    Selects the retry status of an event.
    :param event_id:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT retry FROM events WHERE event_id = ?', (event_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def update_event_retry(event_id, retry, db_path=DB_PATH):
    """
    Updates the retry status of an event in the database.
    :param event_id:
    :param retry:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE events SET retry = ? WHERE event_id = ?', (retry, event_id))
        conn.commit()
    except Exception as e:
        logging.error(f"Error updating event retry status: {e}")
    finally:
        conn.close()


def select_tries(event_id, db_path=DB_PATH):
    """
    Selects the number of tries for an event.
    :param event_id:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT tries FROM events WHERE event_id = ?', (event_id,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        logging.error(f"Error selecting tries: {e}")
        return None
    finally:
        conn.close()


def select_event_uploaded(event_id, db_path=DB_PATH):
    """
    Selects the uploaded status of an event.
    :param event_id:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT uploaded FROM events WHERE event_id = ?', (event_id,))
        result = cursor.fetchone()
        if result:
            uploaded_status = result[0]
            return uploaded_status
        else:
            logging.debug(f"Event ID {event_id} not found in database.")
            return None
    except Exception as e:
        logging.error(f"Error selecting event uploaded status: {e}")
        return None
    finally:
        conn.close()


def select_not_uploaded_yet(db_path=DB_PATH):
    """
    Selects events that are not uploaded yet, retriable, and where created at least 5 minutes ago.
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT event_id FROM events WHERE uploaded = 0 and created <= datetime("now", "-5 minutes") and retry = 1')
        result = cursor.fetchall()
        return [row[0] for row in result]
    except Exception as e:
        logging.error(f"Error selecting not uploaded yet events: {e}")
        return []
    finally:
        conn.close()


def select_not_uploaded_yet_hard(db_path=DB_PATH):
    """
    Selects events that are not uploaded yet and marked as non-retriable (e.g. deleted on Frigate). Use this e.g. for notifying the user.
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT event_id FROM events WHERE uploaded = 0 and created <= datetime("now", "-5 minutes") and retry = 0')
        result = cursor.fetchall()
        return [row[0] for row in result]
    except Exception as e:
        logging.error(f"Error selecting not uploaded yet hard events: {e}")
        return []
    finally:
        conn.close()


def delete_event(event_id, db_path=DB_PATH):
    """
    Permanently deletes an event from the database (e.g. when it no longer exists on Frigate).
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM events WHERE event_id = ?', (event_id,))
        conn.commit()
        logging.debug(f"Deleted event {event_id} from database")
    except Exception as e:
        logging.error(f"Error deleting event {event_id}: {e}")
    finally:
        conn.close()


def get_latest_event_start_time(db_path=DB_PATH):
    """
    Retrieves the start_time of the most recent event from the database.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT MAX(start_time) FROM events')
        result = cursor.fetchone()
        return result[0] if result and result[0] is not None else 0
    except Exception as e:
        logging.error(f"Error getting latest event start time: {e}")
        return 0
    finally:
        conn.close()


def get_health_stats(db_path=DB_PATH):
    """
    Returns a dict with statistics for the daily health report:
      - uploaded_last_24h: events successfully uploaded in last 24h
      - pending_total: events with uploaded=0
      - pending_lt_1d: pending events created in last 24h
      - pending_1d_2d: pending events 1-2 days old
      - pending_2d_3d: pending events 2-3 days old
      - pending_gt_3d: pending events older than 3 days (critical)
      - oldest_pending_age_days: age in days of oldest pending event (None if none)
      - oldest_pending_event_id: id of oldest pending event (None if none)
      - total_uploaded: total successfully uploaded events ever
    """
    stats = {
        "uploaded_last_24h": 0,
        "pending_total": 0,
        "pending_lt_1d": 0,
        "pending_1d_2d": 0,
        "pending_2d_3d": 0,
        "pending_gt_3d": 0,
        "oldest_pending_age_days": None,
        "oldest_pending_event_id": None,
        "total_uploaded": 0,
    }
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE uploaded = 1 AND created >= datetime('now', '-1 day')"
        )
        stats["uploaded_last_24h"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM events WHERE uploaded = 1")
        stats["total_uploaded"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM events WHERE uploaded = 0")
        stats["pending_total"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE uploaded = 0 AND created >= datetime('now', '-1 day')"
        )
        stats["pending_lt_1d"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE uploaded = 0 "
            "AND created < datetime('now', '-1 day') AND created >= datetime('now', '-2 day')"
        )
        stats["pending_1d_2d"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE uploaded = 0 "
            "AND created < datetime('now', '-2 day') AND created >= datetime('now', '-3 day')"
        )
        stats["pending_2d_3d"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE uploaded = 0 AND created < datetime('now', '-3 day')"
        )
        stats["pending_gt_3d"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT event_id, CAST((julianday('now') - julianday(created)) AS REAL) "
            "FROM events WHERE uploaded = 0 ORDER BY created ASC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            stats["oldest_pending_event_id"] = row[0]
            stats["oldest_pending_age_days"] = round(row[1], 1)
    except Exception as e:
        logging.error(f"Error collecting health stats: {e}")
    finally:
        conn.close()
    return stats


def cleanup_old_events(db_path=DB_PATH):
    """
    Deletes ALL events older than DB_RETENTION_DAYS, regardless of upload status.
    Returns the number of deleted rows split by status: (uploaded_deleted, pending_deleted).
    """
    conn = sqlite3.connect(db_path)
    uploaded_deleted = 0
    pending_deleted = 0
    try:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT '
            '  SUM(CASE WHEN uploaded = 1 THEN 1 ELSE 0 END), '
            '  SUM(CASE WHEN uploaded = 0 THEN 1 ELSE 0 END) '
            'FROM events WHERE created <= datetime("now", ? || " days")',
            (f"-{DB_RETENTION_DAYS}",)
        )
        row = cursor.fetchone()
        uploaded_deleted = row[0] or 0
        pending_deleted = row[1] or 0

        cursor.execute(
            'DELETE FROM events WHERE created <= datetime("now", ? || " days")',
            (f"-{DB_RETENTION_DAYS}",)
        )
        conn.commit()

        total = uploaded_deleted + pending_deleted
        if total:
            logging.info(
                f"Cleaned up {total} events older than {DB_RETENTION_DAYS} days "
                f"({uploaded_deleted} uploaded, {pending_deleted} pending)."
            )
    except Exception as e:
        logging.error(f"Error cleaning up old events: {e}")
    finally:
        conn.close()
    return uploaded_deleted, pending_deleted
