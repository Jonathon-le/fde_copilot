import sqlite3
from pathlib import Path
def get_connection(db_path)->sqlite3.Connection :
    conn=sqlite3.connect(db_path)
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL") 
    return conn
def init_db(db_path)->None :
    conn=get_connection(db_path)
    try :
        with open(Path(__file__).with_name("schema.sql"),"r",encoding="utf-8")as f :
            schema_text=f.read()
            conn.executescript(schema_text)
            conn.commit()
    finally :
        conn.close()