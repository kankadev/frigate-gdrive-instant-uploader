import os
import sqlite3
import logging

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db/events.db')


def init_db(db_path=DB_PATH):
    logging.info(f"Initializing database at {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        # Create the events table if it does not exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, 
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


def insert_event(event_id, db_path=DB_PATH):
    """
    Inserts an event into the database.
    :param event_id:
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO events (event_id) VALUES (?)', (event_id,))
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
            logging.debug(f"Event ID {event_id}: Uploaded status is {uploaded_status}")
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
    Selects events that are not uploaded yet and where created at least 5 minutes ago.
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT event_id FROM events WHERE uploaded = 0 and created <= datetime("now", "-5 minutes") and tries <= 5')
        result = cursor.fetchall()
        return [row[0] for row in result]
    except Exception as e:
        logging.error(f"Error selecting not uploaded yet events: {e}")
        return []
    finally:
        conn.close()


def select_not_uploaded_yet_hard(db_path=DB_PATH):
    """
    Selects events that are not uploaded yet and have more than 5 tries. Use this e.g. for notifying the user.
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT event_id FROM events WHERE uploaded = 0 and created <= datetime("now", "-5 minutes") and tries >= 5')
        result = cursor.fetchall()
        return [row[0] for row in result]
    except Exception as e:
        logging.error(f"Error selecting not uploaded yet hard events: {e}")
        return []
    finally:
        conn.close()


def cleanup_old_events(db_path=DB_PATH):
    """
    Deletes uploaded events that are older than 40 days.
    :param db_path:
    :return:
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM events WHERE created <= datetime("now", "-40 days") and uploaded = 1')
        conn.commit()
    except Exception as e:
        logging.error(f"Error cleaning up old events: {e}")
    finally:
        conn.close()
