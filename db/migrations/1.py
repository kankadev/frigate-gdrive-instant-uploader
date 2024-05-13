import logging
import sqlite3
import os

DB_PATH = os.getenv('DB_PATH', 'db/events.db')


def apply_migration():
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # Check if the 'retry' column already exists
        cursor.execute("PRAGMA table_info(events)")
        columns = [column[1] for column in cursor.fetchall()]

        if 'retry' not in columns:
            cursor.execute('''
                ALTER TABLE events ADD COLUMN retry BOOLEAN DEFAULT 1
            ''')
            # Record this migration as applied
            cursor.execute('INSERT INTO migrations (name) VALUES (?)', ('1.py',))

        conn.commit()

    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        conn.close()


if __name__ == "__main__":
    apply_migration()
