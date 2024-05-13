import logging
import sqlite3

from src.database import DB_PATH

logging.debug(f"DB_PATH: {DB_PATH}")


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
        logging.debug("Migration 1.py applied successfully.")

    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        conn.close()


if __name__ == "__main__":
    apply_migration()
