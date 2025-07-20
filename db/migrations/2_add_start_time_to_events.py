import logging
import sqlite3

from src.database import DB_PATH

def apply_migration_2():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        logging.info('Running migration 2_add_start_time_to_events.py...')
        cursor.execute('ALTER TABLE events ADD COLUMN start_time REAL')
        conn.commit()
        logging.info('Migration 2_add_start_time_to_events.py finished successfully.')
    except sqlite3.OperationalError as e:
        if 'duplicate column name' in str(e):
            logging.warning('Column start_time already exists in events table. Skipping.')
        else:
            logging.error(f"Error applying migration 2: {e}")
            raise e
    except Exception as e:
        logging.error(f"An unexpected error occurred during migration 2: {e}")
        raise e
    finally:
        if conn:
            conn.close()

# Run the migration
apply_migration_2()
