import logging
import sqlite3

def apply(cursor):
    """
    Applies the migration to add the start_time column to the events table.
    """
    try:
        cursor.execute('ALTER TABLE events ADD COLUMN start_time REAL')
        logging.info('Migration 2_add_start_time_to_events.py applied successfully.')
    except sqlite3.OperationalError as e:
        # This handles the case where the migration is run more than once
        if 'duplicate column name' in str(e):
            logging.warning('Column start_time already exists. Skipping migration.')
        else:
            # Re-raise the exception if it's a different error
            raise e
