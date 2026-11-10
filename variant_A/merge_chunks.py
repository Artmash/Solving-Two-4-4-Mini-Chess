import sqlite3
import glob

def merge():
    merged_db = sqlite3.connect("chess_graph_merged.db")
    cur = merged_db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS edges (
            from_board_id INTEGER,
            to_board_id INTEGER,
            move TEXT,
            moving_color TEXT,
            PRIMARY KEY (from_board_id, to_board_id, move, moving_color)
        )
    """)
    chunk_files = glob.glob("chess_graph_chunk_*.db")
    total = 0
    for fname in chunk_files:
        print(f"Merging {fname}...")
        conn = sqlite3.connect(fname)
        c = conn.cursor()
        c.execute("SELECT from_board_id, to_board_id, move, moving_color FROM edges")
        rows = c.fetchall()
        cur.executemany("INSERT OR IGNORE INTO edges VALUES (?, ?, ?, ?)", rows)
        merged_db.commit()
        total += len(rows)
        conn.close()
    merged_db.close()
    print(f"Merged database ready: chess_graph_merged.db with {total} edges.")

if __name__ == "__main__":
    merge()