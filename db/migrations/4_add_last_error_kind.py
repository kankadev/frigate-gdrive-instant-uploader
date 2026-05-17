import logging
import sqlite3

from src.database import DB_PATH


def apply_migration_4():
    """
    Adds a `last_error_kind` column to the `events` table.

    The column stores a coarse-grained category for the most recent upload
    failure (e.g. 'frigate_download_timeout', 'drive_5xx', 'clip_too_large').
    It is cleared (set back to NULL) on the next successful upload so that
    the daily health report shows only currently-failing categories.
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        logging.info('Running migration 4_add_last_error_kind.py...')
        cursor.execute('ALTER TABLE events ADD COLUMN last_error_kind TEXT')
        conn.commit()
        logging.info('Migration 4_add_last_error_kind.py finished successfully.')
    except sqlite3.OperationalError as e:
        if 'duplicate column name' in str(e):
            logging.warning('Column last_error_kind already exists in events table. Skipping.')
        else:
            logging.error(f"Error applying migration 4: {e}")
            raise e
    except Exception as e:
        logging.error(f"An unexpected error occurred during migration 4: {e}")
        raise e
    finally:
        if conn:
            conn.close()


# Run the migration
apply_migration_4()
