import sqlite3

DB_PATH = 'db/events.db'


def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, 
            uploaded BOOLEAN NOT NULL CHECK (uploaded IN (0, 1)),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def insert_event(event_id, uploaded, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO events (event_id, uploaded) VALUES (?, ?)', (event_id, uploaded))
    conn.commit()
    conn.close()


def event_uploaded(event_id, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT uploaded FROM events WHERE event_id = ?', (event_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None
