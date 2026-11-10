#!/usr/bin/env python3
# build_reachable_graph_new_variant.py
#
# Builds the reachable game graph for the NBBK/kbbn 4x4 variant.
#   * BFS from the starting board
#   * Prunes at <=3 pieces (terminal DRAW, insufficient material)
#   * Classifies terminal nodes as stalemate (DRAW) or checkmate (LOSS)
#   * Records every reachable (board, side) node and every legal edge
#
# Timing convention matches board_generator_new_variant.py and
# edge_generator_new_variant.py.
# ----------------------------------------------------------------------

import sqlite3
import os
import sys
import time
import argparse
import signal
from collections import deque
import numpy as np

# ============================================================
# Timing helpers
# ============================================================
class Timer:
    def __init__(self):
        self.records = []
        self._open   = {}
    def start(self, name):
        self._open[name] = time.perf_counter()
    def stop(self, name):
        if name in self._open:
            self.records.append((name, time.perf_counter() - self._open.pop(name)))
    def report(self, total_seconds, header="TIMING SUMMARY"):
        print("\n" + "=" * 60)
        print(header)
        print("=" * 60)
        for name, dur in self.records:
            pct = (dur / total_seconds * 100.0) if total_seconds > 0 else 0.0
            print(f"  {name:<22s}: {format_duration(dur)}  ({pct:5.1f}%)")
        print(f"  {'TOTAL':<22s}: {format_duration(total_seconds)}")
        print("=" * 60)

def format_duration(s):
    if s >= 3600:
        h = int(s // 3600); m = int((s % 3600) // 60); s -= h * 3600 + m * 60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if s >= 60:
        m = int(s // 60); s -= m * 60
        return f"{m}m {s:05.2f}s"
    return f"{s:.2f}s"

# ============================================================
# Move generation / attack detection (K, N, B only)
# ============================================================
def str_to_board(s):
    return [list(row) for row in s.split(';')]

def moves_king(board, r, c, color):
    moves = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            nr, nc = r + dr, c + dc
            if 0 <= nr < 4 and 0 <= nc < 4:
                t = board[nr][nc]
                if t == '.' or (color == 'w' and t.islower()) \
                            or (color == 'b' and t.isupper()):
                    moves.append((nr, nc))
    return moves

def moves_knight(board, r, c, color):
    offsets = [(2,1),(2,-1),(-2,1),(-2,-1),(1,2),(1,-2),(-1,2),(-1,-2)]
    moves = []
    for dr, dc in offsets:
        nr, nc = r + dr, c + dc
        if 0 <= nr < 4 and 0 <= nc < 4:
            t = board[nr][nc]
            if t == '.' or (color == 'w' and t.islower()) \
                        or (color == 'b' and t.isupper()):
                moves.append((nr, nc))
    return moves

def moves_bishop(board, r, c, color):
    moves = []
    for dr, dc in ((1,1),(1,-1),(-1,1),(-1,-1)):
        nr, nc = r + dr, c + dc
        while 0 <= nr < 4 and 0 <= nc < 4:
            t = board[nr][nc]
            if t == '.':
                moves.append((nr, nc))
            else:
                if (color == 'w' and t.islower()) \
                or (color == 'b' and t.isupper()):
                    moves.append((nr, nc))
                break
            nr += dr; nc += dc
    return moves

def get_piece_moves(board, r, c, color):
    p = board[r][c]
    if p in ('K', 'k'): return moves_king(board, r, c, color)
    if p in ('N', 'n'): return moves_knight(board, r, c, color)
    if p in ('B', 'b'): return moves_bishop(board, r, c, color)
    return []

def is_attacked(board, r, c, defending_color):
    attacker = 'b' if defending_color == 'w' else 'w'
    for i in range(4):
        for j in range(4):
            p = board[i][j]
            if (attacker == 'w' and p.isupper()) \
            or (attacker == 'b' and p.islower()):
                if (r, c) in get_piece_moves(board, i, j, attacker):
                    return True
    return False

def king_position(board, color):
    k = 'K' if color == 'w' else 'k'
    for r in range(4):
        for c in range(4):
            if board[r][c] == k:
                return r, c
    return None

def king_in_check(board, color):
    pos = king_position(board, color)
    if pos is None:
        return False
    return is_attacked(board, pos[0], pos[1], color)

# ============================================================
# Main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--boards', type=str, default='chess_boards_grouped.db')
    ap.add_argument('--graph',  type=str, default='chess_graph_merged.db')
    ap.add_argument('--out',    type=str, default='reachable_graph.db')
    ap.add_argument('--start-fen', type=str, default='NBBK;....;....;kbbn')
    args = ap.parse_args()

    t_global = time.perf_counter()
    T = Timer()

    print("=" * 60)
    print("REACHABLE GRAPH BUILDER — NBBK/kbbn variant")
    print("=" * 60)
    print(f"Start time (local) : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Boards DB          : {args.boards}")
    print(f"Graph DB           : {args.graph}")
    print(f"Output DB          : {args.out}")
    print(f"Start board string : {args.start_fen}")
    print("=" * 60)

    # ---------------- setup ----------------
    T.start('setup')

    if not os.path.exists(args.boards):
        print(f"ERROR: {args.boards} not found"); sys.exit(1)
    if not os.path.exists(args.graph):
        print(f"ERROR: {args.graph} not found"); sys.exit(1)

    if os.path.exists(args.out):
        print(f"Removing existing {args.out}")
        os.remove(args.out)

    conn_boards = sqlite3.connect(args.boards)
    cur_boards  = conn_boards.cursor()
    cur_boards.execute("PRAGMA cache_size = -65536")

    conn_graph = sqlite3.connect(args.graph)
    cur_graph  = conn_graph.cursor()
    cur_graph.execute("PRAGMA cache_size = -131072")
    cur_graph.execute("PRAGMA query_only = 1")

    # Start board
    cur_boards.execute("SELECT id FROM boards WHERE board = ?", (args.start_fen,))
    row = cur_boards.fetchone()
    if not row:
        print(f"ERROR: start board not found in boards DB: {args.start_fen}")
        sys.exit(1)
    start_board_id = row[0]
    print(f"Start board ID     : {start_board_id}")

    # MAX board id (for the numpy lookup array)
    cur_boards.execute("SELECT MAX(id) FROM boards")
    max_board_id = cur_boards.fetchone()[0]
    print(f"Max board ID       : {max_board_id:,}")

    # Insufficient-material board IDs (piece_count <= 3)
    print("Loading insufficient-material boards ...")
    t = time.perf_counter()
    cur_boards.execute("SELECT id FROM boards WHERE piece_count <= 3")
    insuff_ids = np.zeros(max_board_id + 1, dtype=np.uint8)
    n_insuff_boards = 0
    for (bid,) in cur_boards.fetchall():
        insuff_ids[bid] = 1
        n_insuff_boards += 1
    print(f"  {n_insuff_boards:,} boards with <=3 pieces "
          f"(loaded in {format_duration(time.perf_counter() - t)})")

    # Output DB
    conn_out = sqlite3.connect(args.out)
    cur_out  = conn_out.cursor()
    cur_out.execute("PRAGMA journal_mode = WAL")
    cur_out.execute("PRAGMA synchronous = OFF")
    cur_out.execute("PRAGMA temp_store = MEMORY")

    cur_out.execute("""
        CREATE TABLE nodes (
            node_id         INTEGER PRIMARY KEY,
            board_id        INTEGER NOT NULL,
            side            TEXT    NOT NULL,
            total_children  INTEGER DEFAULT 0,
            win_children    INTEGER DEFAULT 0,
            value           TEXT,
            is_terminal     INTEGER DEFAULT 0,
            draw_type       TEXT,
            depth           INTEGER DEFAULT 0,
            UNIQUE (board_id, side)
        )
    """)
    cur_out.execute("""
        CREATE TABLE edges (
            parent_node_id INTEGER NOT NULL,
            child_node_id  INTEGER NOT NULL,
            move           TEXT    NOT NULL
        )
    """)
    cur_out.execute("""
        CREATE TABLE state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn_out.commit()

    # numpy lookup:  node_id_arr[board_id * 2 + side_bit]
    #   side_bit = 0 for White to move, 1 for Black to move
    #   0 means "not yet discovered"
    node_id_arr = np.zeros(2 * (max_board_id + 1), dtype=np.int32)
    next_node_id = 1

    def side_bit(side):
        return 0 if side == 'w' else 1

    def get_node(bid, side):
        return int(node_id_arr[bid * 2 + side_bit(side)])

    def set_node(bid, side, nid):
        node_id_arr[bid * 2 + side_bit(side)] = nid

    T.stop('setup')

    # ---------------- BFS ----------------
    print("\n=== PHASE: BFS ===")
    T.start('bfs')

    NODE_BATCH = 20000
    EDGE_BATCH = 50000
    UPD_BATCH  = 20000

    node_batch = []      # (node_id, board_id, side, depth)
    edge_batch = []      # (parent_node_id, child_node_id, move)
    upd_batch  = []      # (total_children, is_terminal, draw_type, value, node_id)

    def flush_nodes():
        if node_batch:
            cur_out.executemany(
                "INSERT INTO nodes (node_id, board_id, side, depth) VALUES (?, ?, ?, ?)",
                node_batch)
            node_batch.clear()

    def flush_edges():
        if edge_batch:
            cur_out.executemany(
                "INSERT INTO edges (parent_node_id, child_node_id, move) VALUES (?, ?, ?)",
                edge_batch)
            edge_batch.clear()

    def flush_updates():
        if upd_batch:
            cur_out.executemany("""
                UPDATE nodes
                SET total_children = ?, is_terminal = ?, draw_type = ?, value = ?
                WHERE node_id = ?
            """, upd_batch)
            upd_batch.clear()

    def flush_all(commit=True):
        flush_nodes(); flush_edges(); flush_updates()
        if commit:
            conn_out.commit()

    # Start node
    set_node(start_board_id, 'w', next_node_id)
    start_node_id = next_node_id
    next_node_id += 1
    node_batch.append((start_node_id, start_board_id, 'w', 0))

    # BFS queue
    queue = deque()
    queue.append((start_board_id, 'w', 0))

    # Counters
    n_insufficient = 0
    n_stalemate    = 0
    n_checkmate    = 0
    n_nonterminal  = 0
    n_popped       = 0
    n_edges        = 0

    last_report = time.perf_counter()

    while queue:
        board_id, side, depth = queue.popleft()
        node_id = get_node(board_id, side)
        n_popped += 1

        # -------- terminal by insufficient material --------
        if insuff_ids[board_id]:
            n_insufficient += 1
            upd_batch.append((0, 1, 'insufficient_material', 'DRAW', node_id))
            if len(upd_batch) >= UPD_BATCH:
                flush_updates()
            continue

        # -------- get children (both moves for this side) --------
        cur_graph.execute(
            "SELECT to_board_id, move FROM edges "
            "WHERE from_board_id = ? AND moving_color = ?",
            (board_id, side)
        )
        children = cur_graph.fetchall()
        n_children = len(children)

        # -------- no children: stalemate or checkmate --------
        if n_children == 0:
            cur_boards.execute("SELECT board FROM boards WHERE id = ?", (board_id,))
            row = cur_boards.fetchone()
            board_str = row[0] if row else ''
            board = str_to_board(board_str) if board_str else None
            in_check = king_in_check(board, side) if board else False
            if in_check:
                n_checkmate += 1
                upd_batch.append((0, 1, 'checkmate', 'LOSS', node_id))
            else:
                n_stalemate += 1
                upd_batch.append((0, 1, 'stalemate', 'DRAW', node_id))
            if len(upd_batch) >= UPD_BATCH:
                flush_updates()
            continue

        # -------- non-terminal --------
        n_nonterminal += 1
        upd_batch.append((n_children, 0, None, None, node_id))

        new_side = 'b' if side == 'w' else 'w'
        for to_board_id, move in children:
            child_node_id = get_node(to_board_id, new_side)
            if child_node_id == 0:
                child_node_id = next_node_id
                next_node_id += 1
                set_node(to_board_id, new_side, child_node_id)
                node_batch.append((child_node_id, to_board_id, new_side, depth + 1))
                queue.append((to_board_id, new_side, depth + 1))
                if len(node_batch) >= NODE_BATCH:
                    flush_nodes()
            edge_batch.append((node_id, child_node_id, move))
            n_edges += 1
            if len(edge_batch) >= EDGE_BATCH:
                flush_edges()

        if len(upd_batch) >= UPD_BATCH:
            flush_updates()
        if len(node_batch) >= NODE_BATCH or len(edge_batch) >= EDGE_BATCH:
            conn_out.commit()

        # Progress log every 30 s
        now = time.perf_counter()
        if (now - last_report) > 30:
            elapsed = now - (t_global)
            rate = n_popped / elapsed if elapsed > 0 else 0
            print(f"  BFS: popped {n_popped:>12,} | "
                  f"queue {len(queue):>12,} | "
                  f"nodes {next_node_id - 1:>12,} | "
                  f"edges {n_edges:>14,} | "
                  f"{format_duration(elapsed):>12s} | "
                  f"{rate:>8,.0f} nodes/s", flush=True)
            last_report = now

    flush_all(commit=True)
    T.stop('bfs')

    print(f"\nBFS complete")
    print(f"  Popped          : {n_popped:,}")
    print(f"  Non-terminal    : {n_nonterminal:,}")
    print(f"  Insufficient    : {n_insufficient:,}")
    print(f"  Stalemate       : {n_stalemate:,}")
    print(f"  Checkmate       : {n_checkmate:,}")
    print(f"  Total nodes     : {next_node_id - 1:,}")
    print(f"  Total edges     : {n_edges:,}")

    # ---------------- finalize ----------------
    print("\n=== PHASE: finalize ===")
    T.start('finalize')

    print("  building indexes ...")
    t_idx = time.perf_counter()
    cur_out.execute("CREATE INDEX idx_nodes_board   ON nodes (board_id)")
    cur_out.execute("CREATE INDEX idx_nodes_side    ON nodes (side)")
    cur_out.execute("CREATE INDEX idx_nodes_term    ON nodes (is_terminal)")
    cur_out.execute("CREATE INDEX idx_nodes_value   ON nodes (value)")
    cur_out.execute("CREATE INDEX idx_edges_parent  ON edges (parent_node_id)")
    cur_out.execute("CREATE INDEX idx_edges_child   ON edges (child_node_id)")
    conn_out.commit()
    print(f"  indexes built in {format_duration(time.perf_counter() - t_idx)}")

    # Statistics
    cur_out.executemany(
        "REPLACE INTO state (key, value) VALUES (?, ?)",
        [
            ('start_node_id',         str(start_node_id)),
            ('start_board_id',        str(start_board_id)),
            ('start_fen',             args.start_fen),
            ('total_nodes',           str(next_node_id - 1)),
            ('total_edges',           str(n_edges)),
            ('non_terminal_nodes',    str(n_nonterminal)),
            ('insufficient_material', str(n_insufficient)),
            ('stalemate',             str(n_stalemate)),
            ('checkmate',             str(n_checkmate)),
        ])
    conn_out.commit()
    T.stop('finalize')

    # ---------------- summary ----------------
    total_elapsed = time.perf_counter() - t_global
    print("\n" + "=" * 60)
    print("REACHABLE GRAPH COMPLETE")
    print("=" * 60)
    print(f"End time (local) : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Output DB        : {args.out}")
    print(f"Start FEN        : {args.start_fen}")
    print(f"Start board ID   : {start_board_id}")
    print(f"Start node ID    : {start_node_id}")
    print(f"Total nodes      : {next_node_id - 1:,}")
    print(f"Total edges      : {n_edges:,}")
    print(f"  Non-terminal   : {n_nonterminal:,}")
    print(f"  Insufficient   : {n_insufficient:,}")
    print(f"  Stalemate      : {n_stalemate:,}")
    print(f"  Checkmate      : {n_checkmate:,}")
    if total_elapsed > 0:
        print(f"Throughput       : {n_popped / total_elapsed:,.0f} nodes/s")
    T.report(total_elapsed)

    conn_out.close()
    conn_graph.close()
    conn_boards.close()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))
    main()