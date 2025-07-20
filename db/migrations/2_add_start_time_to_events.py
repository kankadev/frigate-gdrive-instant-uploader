import logging
import sqlite3

logging.info('Running migration 2_add_start_time_to_events.py...')
try:
    cursor.execute('ALTER TABLE events ADD COLUMN start_time REAL')
    logging.info('Migration 2_add_start_time_to_events.py finished successfully.')
except sqlite3.OperationalError as e:
    if 'duplicate column name' in str(e):
        logging.warning('Column start_time already exists. Skipping migration.')
    else:
        raise e
