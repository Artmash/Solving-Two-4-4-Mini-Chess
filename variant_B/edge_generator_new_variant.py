#!/usr/bin/env python3
# edge_generator_new_variant.py
#
# Edge generator for the NEW 4x4 variant:
#   White: K, up to 2 colour-bound bishops, up to 1 knight
#   Black: k, up to 2 colour-bound bishops, up to 1 knight
#
# Reads  : chess_boards_grouped.db      (61,660,012 boards)
# Writes : chess_graph_merged.db        (all directed edges)
#
# Parallelised across N chunks; each chunk has its own temp DB; merged at end.
#
# TIMING: same convention as board_generator_new_variant.py
# ----------------------------------------------------------------------

import sqlite3
import os
import sys
import time
import copy
import argparse
import signal
import multiprocessing as mp

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

def format_duration(seconds):
    if seconds >= 3600:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds - h * 3600 - m * 60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if seconds >= 60:
        m = int(seconds // 60)
        s = seconds - m * 60
        return f"{m}m {s:05.2f}s"
    return f"{seconds:.2f}s"

# ============================================================
# Move generation (K, N, B)
# ============================================================
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
    offsets = [(2,1),(2,-1),(-2,1),(-2,-1),
               (1,2),(1,-2),(-1,2),(-1,-2)]
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
    piece = board[r][c]
    if piece in ('K', 'k'):
        return moves_king(board, r, c, color)
    if piece in ('N', 'n'):
        return moves_knight(board, r, c, color)
    if piece in ('B', 'b'):
        return moves_bishop(board, r, c, color)
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

def is_adjacent_squares(r1, c1, r2, c2):
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1

def legal_moves_for_side(board, side):
    moves = []
    king_r, king_c = king_position(board, side)
    opp = 'b' if side == 'w' else 'w'
    opp_king_r, opp_king_c = king_position(board, opp)
    for r in range(4):
        for c in range(4):
            p = board[r][c]
            if (side == 'w' and p.isupper()) \
            or (side == 'b' and p.islower()):
                for nr, nc in get_piece_moves(board, r, c, side):
                    tmp = copy.deepcopy(board)
                    tmp[nr][nc] = tmp[r][c]
                    tmp[r][c] = '.'
                    if p in ('K', 'k'):
                        if is_adjacent_squares(nr, nc,
                                               opp_king_r, opp_king_c):
                            continue
                    else:
                        if is_adjacent_squares(king_r, king_c,
                                               opp_king_r, opp_king_c):
                            continue
                    if (r, c) == (king_r, king_c):
                        if is_attacked(tmp, nr, nc, side):
                            continue
                    else:
                        if is_attacked(tmp, king_r, king_c, side):
                            continue
                    moves.append((r, c, nr, nc))
    return moves

def apply_move(board, move):
    nb = copy.deepcopy(board)
    r1, c1, r2, c2 = move
    nb[r2][c2] = nb[r1][c1]
    nb[r1][c1] = '.'
    return nb

def board_to_str(board):
    return ';'.join(''.join(row) for row in board)

def str_to_board(s):
    return [list(row) for row in s.split(';')]

def move_to_str(move):
    files = 'abcd'
    r1, c1, r2, c2 = move
    return f"{files[c1]}{r1+1}{files[c2]}{r2+1}"

# ============================================================
# Output DB setup
# ============================================================
def init_output_db(db_path, create_pk=False):
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")
    if create_pk:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                from_board_id INTEGER NOT NULL,
                to_board_id   INTEGER NOT NULL,
                move          TEXT    NOT NULL,
                moving_color  TEXT    NOT NULL,
                PRIMARY KEY (from_board_id, to_board_id, move, moving_color)
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                from_board_id INTEGER NOT NULL,
                to_board_id   INTEGER NOT NULL,
                move          TEXT    NOT NULL,
                moving_color  TEXT    NOT NULL
            )
        """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn, cur

# ============================================================
# Chunk worker  (FIXED: two cursors on the board DB)
# ============================================================
def process_chunk(args):
    (chunk_index, start_id, end_id, board_db_path, out_db_path) = args
    t0 = time.perf_counter()
    pid = os.getpid()
    print(f"  Chunk {chunk_index}: START (ids {start_id}..{end_id}, pid={pid})",
          flush=True)

    # Read-only connection to the board DB.
    # TWO cursors:
    #   board_cur    -> outer iteration over boards
    #   board_lookup -> batched destination-ID lookups
    # Sharing a single cursor would destroy the outer result set on the
    # first lookup, which is exactly the bug that stopped each chunk after
    # only 2,000 boards.
    board_conn   = sqlite3.connect(f"file:{board_db_path}?mode=ro", uri=True)
    board_cur    = board_conn.cursor()
    board_lookup = board_conn.cursor()
    for c in (board_cur, board_lookup):
        c.execute("PRAGMA query_only = 1")
        c.execute("PRAGMA cache_size = -65536")   # ~64 MB per cursor

    out_conn, out_cur = init_output_db(out_db_path, create_pk=False)

    # Resume support
    out_cur.execute("SELECT value FROM progress WHERE key='last_board_id'")
    row = out_cur.fetchone()
    last_id = int(row[0]) if row else 0
    if last_id >= end_id:
        print(f"  Chunk {chunk_index}: already complete (last_id={last_id})",
              flush=True)
        board_conn.close(); out_conn.close()
        return (chunk_index, 0, time.perf_counter() - t0)

    actual_start = max(start_id, last_id + 1)
    print(f"  Chunk {chunk_index}: resuming from id {actual_start}", flush=True)

    # ---- batched destination-ID lookup (uses the SECOND cursor) ----
    def lookup_ids(strings):
        result = {}
        BATCH = 500
        for i in range(0, len(strings), BATCH):
            batch = strings[i:i+BATCH]
            q = ("SELECT board, id FROM boards "
                 "WHERE board IN ({})").format(",".join("?" * len(batch)))
            board_lookup.execute(q, batch)
            for b, bid in board_lookup.fetchall():
                result[b] = bid
        return result

    # ---- outer iteration over boards (uses the FIRST cursor) ----
    board_cur.execute(
        "SELECT id, board FROM boards WHERE id >= ? AND id <= ? ORDER BY id",
        (actual_start, end_id)
    )

    boards_done    = 0
    edges_inserted = 0
    pending_dests  = []   # (src_id, dst_str, move_str, color)
    edge_batch     = []
    last_commit_id = actual_start
    last_report    = t0
    t_chunk        = t0

    while True:
        rows = board_cur.fetchmany(2000)
        if not rows:
            break

        for board_id, board_str in rows:
            board = str_to_board(board_str)
            for side in ('w', 'b'):
                for mv in legal_moves_for_side(board, side):
                    nb = apply_move(board, mv)
                    ds = board_to_str(nb)
                    pending_dests.append((board_id, ds, move_to_str(mv), side))
            boards_done += 1
            last_commit_id = board_id

        # Resolve pending destinations
        if len(pending_dests) >= 2000:
            strings = [d[1] for d in pending_dests]
            id_map  = lookup_ids(strings)         # uses board_lookup
            for src, ds, ms, col in pending_dests:
                dst = id_map.get(ds)
                if dst is None:
                    continue
                edge_batch.append((src, dst, ms, col))
            pending_dests = []

            if len(edge_batch) >= 10000:
                out_cur.executemany(
                    "INSERT INTO edges "
                    "(from_board_id, to_board_id, move, moving_color) "
                    "VALUES (?, ?, ?, ?)", edge_batch)
                edges_inserted += len(edge_batch)
                edge_batch = []
                out_conn.commit()
                out_cur.execute(
                    "REPLACE INTO progress (key, value) VALUES (?, ?)",
                    ('last_board_id', str(last_commit_id)))
                out_conn.commit()

        # Progress log every 30 seconds
        now = time.perf_counter()
        if (now - last_report) > 30:
            elapsed = now - t_chunk
            rate = boards_done / elapsed if elapsed > 0 else 0
            print(f"  Chunk {chunk_index}: {boards_done:>12,} boards, "
                  f"{edges_inserted:>14,} edges | "
                  f"{format_duration(elapsed):>12s} | "
                  f"{rate:>8,.0f} boards/s", flush=True)
            last_report = now

    # Final flush
    if pending_dests:
        strings = [d[1] for d in pending_dests]
        id_map  = lookup_ids(strings)
        for src, ds, ms, col in pending_dests:
            dst = id_map.get(ds)
            if dst is None:
                continue
            edge_batch.append((src, dst, ms, col))
    if edge_batch:
        out_cur.executemany(
            "INSERT INTO edges "
            "(from_board_id, to_board_id, move, moving_color) "
            "VALUES (?, ?, ?, ?)", edge_batch)
        edges_inserted += len(edge_batch)
        out_conn.commit()

    out_cur.execute("REPLACE INTO progress (key, value) VALUES (?, ?)",
                    ('last_board_id', str(end_id)))
    out_conn.commit()

    elapsed = time.perf_counter() - t0
    rate = boards_done / elapsed if elapsed > 0 else 0
    print(f"  Chunk {chunk_index}: DONE  {boards_done:,} boards, "
          f"{edges_inserted:,} edges in {format_duration(elapsed)} "
          f"({rate:,.0f} boards/s)", flush=True)

    board_conn.close(); out_conn.close()
    return (chunk_index, edges_inserted, elapsed)

# ============================================================
# Merge chunk DBs
# ============================================================
def merge_chunks(out_db, chunk_dbs):
    print(f"\n=== PHASE: merge ({len(chunk_dbs)} chunk DBs) ===")
    t0 = time.perf_counter()

    if os.path.exists(out_db):
        os.remove(out_db)

    conn = sqlite3.connect(out_db)
    cur  = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")

    # No PK during bulk load -> fast.
    cur.execute("""
        CREATE TABLE edges (
            from_board_id INTEGER NOT NULL,
            to_board_id   INTEGER NOT NULL,
            move          TEXT    NOT NULL,
            moving_color  TEXT    NOT NULL
        )
    """)
    conn.commit()

    total = 0
    for cdb in chunk_dbs:
        if not os.path.exists(cdb):
            continue
        t1 = time.perf_counter()

        # SQLite forbids parameter binding in ATTACH DATABASE.
        safe_path = cdb.replace("'", "''")
        cur.execute(f"ATTACH DATABASE '{safe_path}' AS chunk")

        # Chunks are disjoint by from_board_id -> no duplicates possible.
        cur.execute("""
            INSERT INTO edges
            SELECT from_board_id, to_board_id, move, moving_color
            FROM chunk.edges
        """)
        conn.commit()
        cur.execute("DETACH DATABASE chunk")

        cur.execute("SELECT COUNT(*) FROM edges")
        total = cur.fetchone()[0]
        print(f"    merged {cdb}: {total:,} edges so far "
              f"(+{format_duration(time.perf_counter() - t1)})", flush=True)

    # Build indexes AFTER bulk load.
    print("  building indexes ...")
    t_idx = time.perf_counter()
    cur.execute("CREATE INDEX idx_from  ON edges (from_board_id)")
    cur.execute("CREATE INDEX idx_to    ON edges (to_board_id)")
    cur.execute("CREATE INDEX idx_color ON edges (moving_color)")
    conn.commit()
    print(f"  indexes built in {format_duration(time.perf_counter() - t_idx)}")

    print("  creating UNIQUE index on (from,to,move,color) ...")
    t_uniq = time.perf_counter()
    cur.execute("""
        CREATE UNIQUE INDEX idx_unique_edge
        ON edges (from_board_id, to_board_id, move, moving_color)
    """)
    conn.commit()
    print(f"  UNIQUE index built in {format_duration(time.perf_counter() - t_uniq)}")

    conn.close()
    print(f"  merge done: {total:,} edges in "
          f"{format_duration(time.perf_counter() - t0)}")
    return total

# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4,
                        help='Number of parallel worker processes')
    parser.add_argument('--chunks',  type=int, default=8,
                        help='Number of chunks to split the board-ID range into')
    parser.add_argument('--boards',  type=str,
                        default='chess_boards_grouped.db',
                        help='Input board database')
    parser.add_argument('--out',     type=str,
                        default='chess_graph_merged.db',
                        help='Output edge database')
    parser.add_argument('--keep-temp', action='store_true',
                        help='Keep per-chunk temp DBs after merge')
    parser.add_argument('--skip-merge', action='store_true',
                        help='Skip merge (leave chunk DBs separate)')
    parser.add_argument('--expected-boards', type=int, default=61_660_012,
                        help='Expected number of boards (skips slow COUNT(*))')
    args = parser.parse_args()

    t_global = time.perf_counter()
    T = Timer()

    print("=" * 60)
    print("EDGE GENERATOR — NEW 4x4 VARIANT")
    print("=" * 60)
    print(f"Start time (local): {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Board DB          : {args.boards}")
    print(f"Output DB         : {args.out}")
    print(f"Workers / chunks  : {args.workers} / {args.chunks}")
    print("=" * 60)

    # ---------------- setup ----------------
    # NOTE: we deliberately avoid COUNT(*) here — it scans the whole
    # 61 M-row table (~40 s on typical disks). MIN/MAX are O(log n).
    T.start('setup')
    if not os.path.exists(args.boards):
        print(f"ERROR: board DB not found: {args.boards}")
        sys.exit(1)

    conn = sqlite3.connect(args.boards)
    cur  = conn.cursor()
    cur.execute("SELECT MIN(id), MAX(id) FROM boards")
    min_id, max_id = cur.fetchone()
    conn.close()
    n_boards = args.expected_boards
    print(f"Board ID range: {min_id}..{max_id}  (~{n_boards:,} boards)")

    n_chunks = max(args.workers, args.chunks)
    span = (max_id - min_id + 1) // n_chunks + 1
    ranges = []
    for i in range(n_chunks):
        s = min_id + i * span
        e = min(max_id, s + span - 1)
        if s > max_id:
            break
        ranges.append((i, s, e))

    chunk_dbs = [f"chess_graph_chunk_{i}.db" for i, _, _ in ranges]
    for cdb in chunk_dbs:
        if os.path.exists(cdb):
            os.remove(cdb)
    T.stop('setup')

    def _sigint(sig, frame):
        print("\nInterrupted.")
        sys.exit(1)
    signal.signal(signal.SIGINT, _sigint)

    # ---------------- generation ----------------
    print("\n=== PHASE: generation (parallel) ===")
    T.start('generation')
    tasks = [(i, s, e, args.boards, chunk_dbs[i]) for i, s, e in ranges]

    results = []
    with mp.Pool(processes=args.workers) as pool:
        for res in pool.imap_unordered(process_chunk, tasks):
            results.append(res)
            running = sum(r[1] for r in results)
            print(f"  -> chunk {res[0]}: {res[1]:,} edges, "
                  f"{format_duration(res[2])} | running total {running:,}",
                  flush=True)
    T.stop('generation')

    # ---------------- merge ----------------
    total_edges = 0
    if not args.skip_merge:
        T.start('merge')
        total_edges = merge_chunks(args.out, chunk_dbs)
        T.stop('merge')
    else:
        print("\n=== PHASE: merge SKIPPED (--skip-merge) ===")
        total_edges = sum(r[1] for r in results)
        print(f"  total edges across all chunks: {total_edges:,}")

    # ---------------- cleanup ----------------
    T.start('cleanup')
    if not args.keep_temp and not args.skip_merge:
        removed = 0
        for cdb in chunk_dbs:
            try:
                os.remove(cdb); removed += 1
            except OSError:
                pass
        print(f"\n=== PHASE: cleanup ===")
        print(f"  removed {removed} temp DBs")
    T.stop('cleanup')

    # ---------------- summary ----------------
    T.start('summary')
    by_color = []
    if not args.skip_merge and os.path.exists(args.out):
        conn = sqlite3.connect(args.out)
        cur  = conn.cursor()
        cur.execute("SELECT moving_color, COUNT(*) FROM edges "
                    "GROUP BY moving_color")
        by_color = cur.fetchall()
        conn.close()
    T.stop('summary')

    total_elapsed = time.perf_counter() - t_global

    print("\n" + "=" * 60)
    print("EDGE GENERATION COMPLETE")
    print("=" * 60)
    print(f"End time (local) : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total edges      : {total_edges:,}")
    if by_color:
        print(f"Edges by colour  :")
        for col, n in by_color:
            print(f"  {col}: {n:,}")
    if total_elapsed > 0:
        print(f"Throughput       : {total_edges / total_elapsed:,.0f} edges/s (wall)")
        print(f"                  {total_edges / total_elapsed / 1e6:.2f} M edges/s")

    print("\nPer-chunk worker times (descending):")
    for r in sorted(results, key=lambda x: -x[2]):
        print(f"  chunk {r[0]}: {r[1]:>14,} edges in {format_duration(r[2])}")

    T.report(total_elapsed)

if __name__ == "__main__":
    main()