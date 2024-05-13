import logging
import sqlite3
import os

DB_PATH = os.getenv('DB_PATH', 'db/events.db')


def apply_migration():
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM migrations WHERE name='1.py'")
        result = cursor.fetchone()

        if result:
            logging.debug("Migration 1.py already applied.")
            return

        cursor.execute('ALTER TABLE events ADD COLUMN retry BOOLEAN DEFAULT 1')
        conn.commit()

    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        conn.close()


if __name__ == "__main__":
    apply_migration()
