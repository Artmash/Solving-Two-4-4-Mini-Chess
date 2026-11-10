import sqlite3
import glob

def merge_databases():
    merged_db = "chess_boards_grouped.db"
    
    # Create merged database
    conn = sqlite3.connect(merged_db)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            piece_count INTEGER,
            piece_type TEXT,
            board TEXT UNIQUE
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_piece_count ON boards (piece_count)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_piece_type ON boards (piece_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_piece_count_type ON boards (piece_count, piece_type)")
    
    # Get all state databases
    state_files = glob.glob("chess_boards_state_*.db")
    state_files.sort()
    
    total = 0
    for fname in state_files:
        print(f"Merging {fname}...")
        conn_state = sqlite3.connect(fname)
        cur_state = conn_state.cursor()
        
        # Insert all boards from this state
        cur_state.execute("SELECT piece_count, piece_type, board FROM boards")
        rows = cur_state.fetchall()
        cur.executemany(
            "INSERT OR IGNORE INTO boards (piece_count, piece_type, board) VALUES (?, ?, ?)",
            rows
        )
        conn.commit()
        total += len(rows)
        conn_state.close()
    
    print(f"\nMerged database ready: {merged_db} with {total} boards.")
    conn.close()

if __name__ == "__main__":
    merge_databases()