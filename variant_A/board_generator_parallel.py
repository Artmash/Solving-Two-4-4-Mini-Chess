import sqlite3
import itertools
import time
import os
import signal
import sys
import argparse
from itertools import combinations

# ------------------------------------------------------------
# Board geometry and helper functions
# ------------------------------------------------------------
SQUARES = list(range(16))

def is_adjacent(i, j):
    r1, c1 = divmod(i, 4)
    r2, c2 = divmod(j, 4)
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1 and (r1 != r2 or c1 != c2)

def square_color(i):
    return (i // 4 + i % 4) % 2   # 0 = light, 1 = dark

def board_to_str(occupied):
    board = [['.' for _ in range(4)] for _ in range(4)]
    for sq, ch in occupied:
        r, c = divmod(sq, 4)
        board[r][c] = ch
    return ';'.join(''.join(row) for row in board)

def count_pieces(board_str):
    return sum(1 for ch in board_str if ch != '.' and ch != ';')

def get_piece_type(board_str):
    pieces = [ch for ch in board_str if ch != '.' and ch != ';']
    return ''.join(sorted(pieces))

# ------------------------------------------------------------
# Generate all ordered non‑adjacent king placements
# ------------------------------------------------------------
def generate_king_placements():
    kings = []
    for wk in SQUARES:
        for bk in SQUARES:
            if wk == bk or is_adjacent(wk, bk):
                continue
            kings.append((wk, bk))
    return kings

KING_PLACEMENTS = generate_king_placements()
print(f"Total king placements: {len(KING_PLACEMENTS)}")  # 156

# ------------------------------------------------------------
# Database setup with progress tracking and grouping
# ------------------------------------------------------------
def init_database(db_path):
    conn = sqlite3.connect(db_path)
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
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn, cur

def get_last_progress(cur):
    keys = ['last_king_idx', 'last_light_idx', 'last_dark_idx',
            'last_a', 'last_b', 'last_w_comb_idx', 'last_b_comb_idx']
    values = []
    for key in keys:
        cur.execute("SELECT value FROM progress WHERE key = ?", (key,))
        row = cur.fetchone()
        values.append(int(row[0]) if row else -1)
    return tuple(values)

def save_progress(conn, cur, king_idx, light_idx, dark_idx, a, b, w_comb_idx, b_comb_idx):
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_king_idx', str(king_idx)))
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_light_idx', str(light_idx)))
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_dark_idx', str(dark_idx)))
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_a', str(a)))
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_b', str(b)))
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_w_comb_idx', str(w_comb_idx)))
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_b_comb_idx', str(b_comb_idx)))
    conn.commit()
    print(f"✓ Progress saved: state={STATE}, king={king_idx}, light={light_idx}, dark={dark_idx}, a={a}, b={b}")

# ------------------------------------------------------------
# Batch insertion
# ------------------------------------------------------------
BATCH_SIZE = 10000
batch = []
total_inserted = 0

def flush_batch(conn, cur):
    global batch, total_inserted
    if batch:
        cur.executemany("INSERT OR IGNORE INTO boards (piece_count, piece_type, board) VALUES (?, ?, ?)", batch)
        conn.commit()
        total_inserted += len(batch)
        print(f"Inserted {len(batch)} boards. Total so far: {total_inserted}")
        batch = []

# ------------------------------------------------------------
# Core generation with piece type computation
# ------------------------------------------------------------
def add_knights_and_store(conn, cur, base_occupied, piece_count_base, empty_squares,
                          king_idx, light_idx, dark_idx, a_start, b_start,
                          w_comb_start, b_comb_start):
    global batch
    
    # Fix: ensure starting indices are at least 0
    if a_start < 0:
        a_start = 0
    if b_start < 0:
        b_start = 0
    if w_comb_start < 0:
        w_comb_start = 0
    if b_comb_start < 0:
        b_comb_start = 0
    
    comb_cache = {}
    for a in range(3):
        comb_cache[a] = list(combinations(empty_squares, a))
    
    for a in range(a_start, 3):
        white_combinations = comb_cache[a]
        for wi, white_knights in enumerate(white_combinations):
            if a == a_start and wi < w_comb_start:
                continue
            remaining1 = [s for s in empty_squares if s not in white_knights]
            comb_cache_b = {}
            for b in range(3):
                comb_cache_b[b] = list(combinations(remaining1, b))
            for b in range(b_start if a == a_start else 0, 3):
                black_combinations = comb_cache_b[b]
                for bi, black_knights in enumerate(black_combinations):
                    if a == a_start and b == b_start and bi < b_comb_start:
                        continue
                    piece_count = piece_count_base + a + b
                    occupied = base_occupied.copy()
                    for sq in white_knights:
                        occupied.append((sq, 'N'))
                    for sq in black_knights:
                        occupied.append((sq, 'n'))
                    board_str = board_to_str(occupied)
                    piece_type = get_piece_type(board_str)
                    batch.append((piece_count, piece_type, board_str))
                    if len(batch) >= BATCH_SIZE:
                        flush_batch(conn, cur)
                        save_progress(conn, cur, king_idx, light_idx, dark_idx,
                                      a, b, wi, bi)
            b_start = 0
        a_start = 0
    flush_batch(conn, cur)
    # Final state save for this combination
    save_progress(conn, cur, king_idx, light_idx, dark_idx, 2, 2, 0, 0)

def print_summary(cur, state):
    """Print grouped counts and total boards."""
    cur.execute("SELECT piece_count, piece_type, COUNT(*) FROM boards GROUP BY piece_count, piece_type ORDER BY piece_count, piece_type")
    groups = cur.fetchall()
    print("\n" + "="*60)
    print(f"State {state} generation complete! Grouped counts:")
    print("="*60)
    total = 0
    for pc, pt, cnt in groups:
        print(f"  {pc} pieces, type {pt}: {cnt:,} boards")
        total += cnt
    print(f"\nTotal boards in state {state}: {total:,}")

# ------------------------------------------------------------
# Main generation loop for a specific bishop state
# ------------------------------------------------------------
def signal_handler(sig, frame):
    print("\nInterrupted. Saving progress before exit...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def generate_state(state, db_path):
    conn, cur = init_database(db_path)
    
    last_king, last_light, last_dark, last_a, last_b, last_wc, last_bc = get_last_progress(cur)
    print(f"State {state}: Loaded progress: king={last_king}, light={last_light}, dark={last_dark}, a={last_a}, b={last_b}")
    
    start_king = last_king + 1 if last_king >= 0 else 0
    print(f"State {state}: Resuming from king index: {start_king}")
    
    if start_king >= len(KING_PLACEMENTS):
        print(f"State {state}: All king placements already processed.")
        print_summary(cur, state)
        conn.close()
        return
    
    LIGHT = [s for s in SQUARES if square_color(s) == 0]
    DARK = [s for s in SQUARES if square_color(s) == 1]
    
    try:
        for ki, (wk, bk) in enumerate(KING_PLACEMENTS):
            if ki < start_king:
                continue
            
            light_left = [s for s in LIGHT if s not in (wk, bk)]
            dark_left = [s for s in DARK if s not in (wk, bk)]
            
            if state == 0:
                # No bishops
                base_occupied = [(wk, 'K'), (bk, 'k')]
                empty = [s for s in SQUARES if s not in (wk, bk)]
                add_knights_and_store(conn, cur, base_occupied, 2, empty,
                                      ki, -1, -1,
                                      last_a if ki == start_king else 0,
                                      last_b if ki == start_king else 0,
                                      last_wc if ki == start_king else 0,
                                      last_bc if ki == start_king else 0)
            
            elif state == 1:
                # White bishop only (on light squares)
                for li, wb in enumerate(light_left):
                    if ki == start_king and li < last_light:
                        continue
                    base_occupied = [(wk, 'K'), (bk, 'k'), (wb, 'B')]
                    empty = [s for s in SQUARES if s not in (wk, bk, wb)]
                    add_knights_and_store(conn, cur, base_occupied, 3, empty,
                                          ki, li, -1,
                                          last_a if ki == start_king and li == last_light else 0,
                                          last_b if ki == start_king and li == last_light else 0,
                                          last_wc if ki == start_king and li == last_light else 0,
                                          last_bc if ki == start_king and li == last_light else 0)
            
            elif state == 2:
                # Black bishop only (on dark squares)
                for di, bb in enumerate(dark_left):
                    if ki == start_king and di < last_dark:
                        continue
                    base_occupied = [(wk, 'K'), (bk, 'k'), (bb, 'b')]
                    empty = [s for s in SQUARES if s not in (wk, bk, bb)]
                    add_knights_and_store(conn, cur, base_occupied, 3, empty,
                                          ki, -1, di,
                                          last_a if ki == start_king and di == last_dark else 0,
                                          last_b if ki == start_king and di == last_dark else 0,
                                          last_wc if ki == start_king and di == last_dark else 0,
                                          last_bc if ki == start_king and di == last_dark else 0)
            
            elif state == 3:
                # Both bishops (white on light, black on dark)
                for li, wb in enumerate(light_left):
                    if ki == start_king and li < last_light:
                        continue
                    for di, bb in enumerate(dark_left):
                        if ki == start_king and li == last_light and di < last_dark:
                            continue
                        base_occupied = [(wk, 'K'), (bk, 'k'), (wb, 'B'), (bb, 'b')]
                        empty = [s for s in SQUARES if s not in (wk, bk, wb, bb)]
                        add_knights_and_store(conn, cur, base_occupied, 4, empty,
                                              ki, li, di,
                                              last_a if ki == start_king and li == last_light and di == last_dark else 0,
                                              last_b if ki == start_king and li == last_light and di == last_dark else 0,
                                              last_wc if ki == start_king and li == last_light and di == last_dark else 0,
                                              last_bc if ki == start_king and li == last_light and di == last_dark else 0)
            
            # After finishing a king placement, reset progress for the next one
            last_light = -1
            last_dark = -1
            last_a = 0
            last_b = 0
            last_wc = 0
            last_bc = 0
            # Save progress to indicate this king is fully done
            save_progress(conn, cur, ki, -1, -1, 2, 2, 0, 0)
        
        # If we get here, all kings are done
        flush_batch(conn, cur)
        save_progress(conn, cur, len(KING_PLACEMENTS)-1, -1, -1, 2, 2, 0, 0)
        print(f"State {state}: All king placements processed.")
        
    except KeyboardInterrupt:
        print(f"\nState {state}: KeyboardInterrupt caught. Progress saved after last batch.")
    finally:
        print_summary(cur, state)
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=int, required=True, choices=[0, 1, 2, 3],
                        help='Bishop state: 0=none, 1=white only, 2=black only, 3=both')
    args = parser.parse_args()
    
    STATE = args.state
    DB_PATH = f"chess_boards_state_{STATE}.db"
    
    print(f"Starting generation for state {STATE}...")
    generate_state(STATE, DB_PATH)