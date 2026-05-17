import logging
import sqlite3

from src.database import DB_PATH


def apply_migration_3():
    """
    Adds two partial indexes to speed up the retry-queue selectors:

      - idx_pending_retry: serves select_not_uploaded_yet()
            WHERE uploaded = 0 AND retry = 1 ORDER BY created ASC
      - idx_pending_hard:  serves select_not_uploaded_yet_hard()
            WHERE uploaded = 0 AND retry = 0

    Partial indexes are used instead of a composite index over
    (uploaded, retry, created) because the vast majority of rows have
    uploaded = 1 and would only bloat the index without ever being read.
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        logging.info('Running migration 3_add_pending_index.py...')

        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_pending_retry '
            'ON events (created) '
            'WHERE uploaded = 0 AND retry = 1'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_pending_hard '
            'ON events (created) '
            'WHERE uploaded = 0 AND retry = 0'
        )

        conn.commit()
        logging.info('Migration 3_add_pending_index.py finished successfully.')
    except Exception as e:
        logging.error(f"An unexpected error occurred during migration 3: {e}")
        raise e
    finally:
        if conn:
            conn.close()


# Run the migration
apply_migration_3()
