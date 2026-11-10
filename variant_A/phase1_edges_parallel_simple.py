import sqlite3
import copy
import argparse
import time
import sys

# ------------------------------------------------------------
# Move generation functions (standard)
# ------------------------------------------------------------
def moves_king(board, r, c, color):
    moves = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            nr, nc = r + dr, c + dc
            if 0 <= nr < 4 and 0 <= nc < 4:
                target = board[nr][nc]
                if target == '.' or (color == 'w' and target.islower()) or (color == 'b' and target.isupper()):
                    moves.append((nr, nc))
    return moves

def moves_knight(board, r, c, color):
    offsets = [(2,1),(2,-1),(-2,1),(-2,-1),(1,2),(1,-2),(-1,2),(-1,-2)]
    moves = []
    for dr, dc in offsets:
        nr, nc = r + dr, c + dc
        if 0 <= nr < 4 and 0 <= nc < 4:
            target = board[nr][nc]
            if target == '.' or (color == 'w' and target.islower()) or (color == 'b' and target.isupper()):
                moves.append((nr, nc))
    return moves

def moves_bishop(board, r, c, color):
    moves = []
    for dr, dc in ((1,1),(1,-1),(-1,1),(-1,-1)):
        nr, nc = r + dr, c + dc
        while 0 <= nr < 4 and 0 <= nc < 4:
            target = board[nr][nc]
            if target == '.':
                moves.append((nr, nc))
            else:
                if (color == 'w' and target.islower()) or (color == 'b' and target.isupper()):
                    moves.append((nr, nc))
                break
            nr += dr
            nc += dc
    return moves

def get_piece_moves(board, r, c, color):
    piece = board[r][c]
    if piece == 'K' or piece == 'k':
        return moves_king(board, r, c, color)
    elif piece == 'N' or piece == 'n':
        return moves_knight(board, r, c, color)
    elif piece == 'B' or piece == 'b':
        return moves_bishop(board, r, c, color)
    else:
        return []

def is_attacked(board, r, c, defending_color):
    attacker_color = 'b' if defending_color == 'w' else 'w'
    for i in range(4):
        for j in range(4):
            piece = board[i][j]
            if (attacker_color == 'w' and piece.isupper()) or (attacker_color == 'b' and piece.islower()):
                moves = get_piece_moves(board, i, j, attacker_color)
                if (r, c) in moves:
                    return True
    return False

def king_position(board, color):
    king_char = 'K' if color == 'w' else 'k'
    for r in range(4):
        for c in range(4):
            if board[r][c] == king_char:
                return r, c
    return None

def is_adjacent_squares(r1, c1, r2, c2):
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1

def legal_moves_for_side(board, side):
    moves = []
    king_r, king_c = king_position(board, side)
    opp_side = 'b' if side == 'w' else 'w'
    opp_king_r, opp_king_c = king_position(board, opp_side)
    for r in range(4):
        for c in range(4):
            piece = board[r][c]
            if (side == 'w' and piece.isupper()) or (side == 'b' and piece.islower()):
                for nr, nc in get_piece_moves(board, r, c, side):
                    temp_board = copy.deepcopy(board)
                    temp_board[nr][nc] = temp_board[r][c]
                    temp_board[r][c] = '.'
                    if piece == 'K' or piece == 'k':
                        if is_adjacent_squares(nr, nc, opp_king_r, opp_king_c):
                            continue
                    else:
                        if is_adjacent_squares(king_r, king_c, opp_king_r, opp_king_c):
                            continue
                    if (r, c) == (king_r, king_c):
                        if is_attacked(temp_board, nr, nc, side):
                            continue
                    else:
                        if is_attacked(temp_board, king_r, king_c, side):
                            continue
                    moves.append((r, c, nr, nc))
    return moves

def apply_move(board, move):
    new_board = copy.deepcopy(board)
    from_r, from_c, to_r, to_c = move
    new_board[to_r][to_c] = new_board[from_r][from_c]
    new_board[from_r][from_c] = '.'
    return new_board

def board_to_str(board):
    return ';'.join(''.join(row) for row in board)

def str_to_board(s):
    rows = s.split(';')
    return [list(row) for row in rows]

def move_to_str(move):
    files = 'abcd'
    r1, c1, r2, c2 = move
    return f"{files[c1]}{r1+1}{files[c2]}{r2+1}"

# ------------------------------------------------------------
# Dead draw detection
# ------------------------------------------------------------
def is_dead_draw(board_str):
    """Return True if the position is a dead draw (insufficient mating material)."""
    pieces = [ch for ch in board_str if ch != '.' and ch != ';']
    white = [p for p in pieces if p.isupper()]
    black = [p for p in pieces if p.islower()]
    # Only kings
    if len(white) == 1 and white[0] == 'K' and len(black) == 1 and black[0] == 'k':
        return True
    # One side has king + exactly one minor, other side only king
    if len(white) == 2 and len(black) == 1 and black[0] == 'k':
        if all(p in ('K','B','N') for p in white):
            return True
    if len(black) == 2 and len(white) == 1 and white[0] == 'K':
        if all(p in ('k','b','n') for p in black):
            return True
    # Both sides have king + one minor (bishop or knight) each, no other pieces
    if len(white) == 2 and len(black) == 2:
        if all(p in ('K','B','N') for p in white) and all(p in ('k','b','n') for p in black):
            return True
    return False

# ------------------------------------------------------------
# Progress handling inside each chunk database
# ------------------------------------------------------------
def get_last_progress(cur):
    cur.execute("SELECT value FROM progress WHERE key='last_board_id'")
    row = cur.fetchone()
    return int(row[0]) if row else 0

def save_progress(conn, cur, board_id):
    cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", ('last_board_id', str(board_id)))
    conn.commit()

# ------------------------------------------------------------
# Main edge generation for a chunk of board IDs
# ------------------------------------------------------------
def process_chunk(start_id, end_id, chunk_index, board_db_path="chess_boards_grouped.db"):
    board_db = sqlite3.connect(board_db_path)
    board_db.row_factory = sqlite3.Row
    cur_board = board_db.cursor()

    graph_db_name = f"chess_graph_chunk_{chunk_index}.db"
    graph_db = sqlite3.connect(graph_db_name)
    cur_graph = graph_db.cursor()

    # Create edges table and progress table
    cur_graph.execute("""
        CREATE TABLE IF NOT EXISTS edges (
            from_board_id INTEGER,
            to_board_id INTEGER,
            move TEXT,
            moving_color TEXT,
            PRIMARY KEY (from_board_id, to_board_id, move, moving_color)
        )
    """)
    cur_graph.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    graph_db.commit()

    # Read last processed board ID from this chunk
    last_id = get_last_progress(cur_graph)
    print(f"Chunk {chunk_index}: Resuming from board ID {last_id}")

    # Build mapping from board string to id (for lookup)
    cur_board.execute("SELECT id, board FROM boards")
    board_to_id = {board: id for id, board in cur_board.fetchall()}

    # Get boards in this range, skipping already processed ones
    cur_board.execute("""
        SELECT id, board, piece_type
        FROM boards
        WHERE id BETWEEN ? AND ? AND id > ?
        ORDER BY id
    """, (start_id, end_id, last_id))
    boards = cur_board.fetchall()
    total = len(boards)
    if total == 0:
        print(f"Chunk {chunk_index}: No new boards to process (all done).")
        board_db.close()
        graph_db.close()
        return

    print(f"Chunk {chunk_index}: {total} boards remaining to process (IDs > {last_id})")

    edge_batch = []
    total_edges = 0
    processed = 0
    skipped_draw = 0
    start_time = time.time()
    last_commit_time = time.time()

    # We'll commit progress after every 1000 boards or 60 seconds
    commit_interval_boards = 1000
    commit_interval_seconds = 60

    for row in boards:
        board_id = row['id']
        board_str = row['board']

        # Skip dead draws
        if is_dead_draw(board_str):
            skipped_draw += 1
            processed += 1
            if processed % 10000 == 0:
                print(f"Chunk {chunk_index}: Processed {processed}/{total}, skipped draws: {skipped_draw}")
            # Save progress even for skipped boards to avoid re-skipping after restart
            if processed % commit_interval_boards == 0:
                save_progress(graph_db, cur_graph, board_id)
            continue

        board = str_to_board(board_str)
        for side in ('w', 'b'):
            moves = legal_moves_for_side(board, side)
            for move in moves:
                new_board = apply_move(board, move)
                dest_board_str = board_to_str(new_board)
                dest_id = board_to_id.get(dest_board_str)
                if dest_id is None:
                    continue
                edge_batch.append((board_id, dest_id, move_to_str(move), side))
                total_edges += 1

                if len(edge_batch) >= 5000:
                    cur_graph.executemany("INSERT OR IGNORE INTO edges (from_board_id, to_board_id, move, moving_color) VALUES (?, ?, ?, ?)", edge_batch)
                    graph_db.commit()
                    edge_batch.clear()
                    print(f"Chunk {chunk_index}: Inserted batch. Total edges: {total_edges}")

        processed += 1
        if processed % 100 == 0:
            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            print(f"Chunk {chunk_index}: Processed {processed}/{total}, skipped draws: {skipped_draw}, rate: {rate:.1f} boards/sec")

        # Save progress periodically
        if processed % commit_interval_boards == 0:
            save_progress(graph_db, cur_graph, board_id)
        elif time.time() - last_commit_time > commit_interval_seconds:
            save_progress(graph_db, cur_graph, board_id)
            last_commit_time = time.time()

    # Final flush of edges
    if edge_batch:
        cur_graph.executemany("INSERT OR IGNORE INTO edges (from_board_id, to_board_id, move, moving_color) VALUES (?, ?, ?, ?)", edge_batch)
        graph_db.commit()
        print(f"Chunk {chunk_index}: Final flush. Total edges: {total_edges}")

    # Save final progress (mark as complete by saving the end_id)
    save_progress(graph_db, cur_graph, end_id)
    print(f"Chunk {chunk_index}: Completed up to ID {end_id}")

    board_db.close()
    graph_db.close()
    print(f"Chunk {chunk_index} complete. Edges: {total_edges}, skipped draws: {skipped_draw}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', type=int, required=True, help='First board ID (inclusive)')
    parser.add_argument('--end', type=int, required=True, help='Last board ID (inclusive)')
    parser.add_argument('--chunk', type=int, required=True, help='Chunk index (0-based)')
    args = parser.parse_args()
    process_chunk(args.start, args.end, args.chunk)