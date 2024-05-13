import sqlite3
import os

DB_PATH = os.getenv('DB_PATH', 'db/events.db')


def apply_migration():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Add the retry column if it doesn't exist
    cursor.execute("ALTER TABLE events ADD COLUMN retry BOOLEAN DEFAULT 1")

    conn.commit()
    conn.close()
    print("Migration applied successfully.")


if __name__ == "__main__":
    apply_migration()
