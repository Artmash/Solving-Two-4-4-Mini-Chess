#!/usr/bin/env python3
# board_generator_new_variant.py
#
# Board generator for the new 4x4 variant:
#   White: King, up to 2 colour-bound bishops, up to 1 knight
#   Black: King, up to 2 colour-bound bishops, up to 1 knight
#
# Parallelised across 4 cores using 16 bishop-state combinations:
#   state 0 = no bishops
#   state 1 = 1 bishop on light square
#   state 2 = 1 bishop on dark square
#   state 3 = 2 bishops (one light, one dark)
#
# Each task writes to its own temp DB and all are merged at the end.
# Expected total: 61,660,012 legal boards.
#
# TIMING: this script uses a consistent timing convention.
#   - `Timer` class for phase-level wall-clock measurements.
#   - `t_start` captured at the very top of `main()`.
#   - Final report prints per-phase durations and percentages.
# --------------------------------------------------------------------

import sqlite3
import time
import os
import signal
import sys
import argparse
import multiprocessing as mp
from datetime import timedelta

# ------------------------------------------------------------
# Timing helper
# ------------------------------------------------------------
class Timer:
    """Simple phase timer. Usage:
        T = Timer()
        T.start('generation')
        ...
        T.stop('generation')
        T.report()
    """
    def __init__(self):
        self.records = []          # (name, duration_seconds)
        self._open   = {}          # name -> start_time

    def start(self, name):
        self._open[name] = time.perf_counter()

    def stop(self, name):
        if name not in self._open:
            return
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
    """Human-readable duration: 1h 23m 45.6s, or 45.6s, or 12.3 ms."""
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

# ------------------------------------------------------------
# Board geometry
# ------------------------------------------------------------
SQUARES       = list(range(16))
LIGHT_SQUARES = [s for s in SQUARES if (s // 4 + s % 4) % 2 == 0]
DARK_SQUARES  = [s for s in SQUARES if (s // 4 + s % 4) % 2 == 1]

def is_adjacent(i, j):
    r1, c1 = divmod(i, 4)
    r2, c2 = divmod(j, 4)
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1 and (r1 != r2 or c1 != c2)

def board_to_str(occupied):
    board = [['.' for _ in range(4)] for _ in range(4)]
    for sq, ch in occupied:
        r, c = divmod(sq, 4)
        board[r][c] = ch
    return ';'.join(''.join(row) for row in board)

def get_piece_type(board_str):
    pieces = [ch for ch in board_str if ch != '.' and ch != ';']
    return ''.join(sorted(pieces))

# ------------------------------------------------------------
# King placements: 156 ordered non-adjacent pairs
# ------------------------------------------------------------
def generate_king_placements():
    kings = []
    for wk in SQUARES:
        for bk in SQUARES:
            if wk == bk or is_adjacent(wk, bk):
                continue
            kings.append((wk, bk))
    return kings

KING_PLACEMENTS = generate_king_placements()   # 156 placements

# ------------------------------------------------------------
# Bishop placement helper
# ------------------------------------------------------------
def bishop_placements(state, light_avail, dark_avail, bishop_char):
    """
    Return a list of 'occupied lists'. Each occupied list contains the
    (square, piece_char) tuples for this side's bishops.
    """
    if state == 0:                       # no bishops
        return [[]]
    if state == 1:                       # one bishop on a light square
        return [[(sq, bishop_char)] for sq in light_avail]
    if state == 2:                       # one bishop on a dark square
        return [[(sq, bishop_char)] for sq in dark_avail]
    if state == 3:                       # both bishops
        return [[(wl, bishop_char), (wd, bishop_char)]
                for wl in light_avail
                for wd in dark_avail]
    raise ValueError(f"Invalid bishop state: {state}")

# ------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------
def init_database(db_path):
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            piece_count INTEGER,
            piece_type  TEXT,
            board       TEXT UNIQUE
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_piece_count ON boards (piece_count)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_piece_type  ON boards (piece_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pc_type     ON boards (piece_count, piece_type)")
    conn.commit()
    return conn, cur

BATCH_SIZE = 10000

class BatchInserter:
    def __init__(self, conn, cur):
        self.conn  = conn
        self.cur   = cur
        self.batch = []
        self.total = 0
    def add(self, row):
        self.batch.append(row)
        if len(self.batch) >= BATCH_SIZE:
            self.flush()
    def flush(self):
        if not self.batch:
            return
        self.cur.executemany(
            "INSERT OR IGNORE INTO boards (piece_count, piece_type, board) "
            "VALUES (?, ?, ?)", self.batch)
        self.conn.commit()
        self.total += len(self.batch)
        self.batch.clear()

# ------------------------------------------------------------
# Worker: one (W_bishop_state, B_bishop_state) combination
# ------------------------------------------------------------
def worker(args):
    W_bs, B_bs, temp_db = args
    t0 = time.perf_counter()
    print(f"  [W={W_bs}, B={B_bs}] START  (pid={os.getpid()})", flush=True)

    conn, cur = init_database(temp_db)
    ins = BatchInserter(conn, cur)

    count = 0
    kings_done = 0
    last_report = t0

    for ki, (wk, bk) in enumerate(KING_PLACEMENTS):
        light_avail = [s for s in LIGHT_SQUARES if s != wk and s != bk]
        dark_avail  = [s for s in DARK_SQUARES  if s != wk and s != bk]

        W_placements = bishop_placements(W_bs, light_avail, dark_avail, 'B')
        B_placements = bishop_placements(B_bs, light_avail, dark_avail, 'b')

        for W_occ in W_placements:
            W_sqs = {sq for sq, _ in W_occ}
            for B_occ in B_placements:
                B_sqs = {sq for sq, _ in B_occ}
                if W_sqs & B_sqs:
                    continue  # bishops cannot share a square

                base_occ = [(wk, 'K'), (bk, 'k')] + W_occ + B_occ
                occupied = {wk, bk} | W_sqs | B_sqs
                empty    = [s for s in SQUARES if s not in occupied]
                pc_base  = len(base_occ)

                # No knights
                bstr = board_to_str(base_occ)
                ins.add((pc_base, get_piece_type(bstr), bstr)); count += 1

                # Only White knight
                for wn in empty:
                    occ = base_occ + [(wn, 'N')]
                    bstr = board_to_str(occ)
                    ins.add((pc_base + 1, get_piece_type(bstr), bstr)); count += 1

                # Only Black knight
                for bn in empty:
                    occ = base_occ + [(bn, 'n')]
                    bstr = board_to_str(occ)
                    ins.add((pc_base + 1, get_piece_type(bstr), bstr)); count += 1

                # Both knights (distinct: N and n)
                for wn in empty:
                    for bn in empty:
                        if wn == bn:
                            continue
                        occ = base_occ + [(wn, 'N'), (bn, 'n')]
                        bstr = board_to_str(occ)
                        ins.add((pc_base + 2, get_piece_type(bstr), bstr)); count += 1

        kings_done += 1

        # Progress report every 20 kings (or every ~30 seconds)
        now = time.perf_counter()
        if kings_done % 20 == 0 or (now - last_report) > 30:
            elapsed = now - t0
            rate = count / elapsed if elapsed > 0 else 0
            print(f"  [W={W_bs}, B={B_bs}] "
                  f"kings {kings_done:>3d}/156 | "
                  f"{count:>12,} boards | "
                  f"{format_duration(elapsed):>12s} | "
                  f"{rate:>10,.0f} boards/s",
                  flush=True)
            last_report = now

    ins.flush()
    conn.close()

    elapsed = time.perf_counter() - t0
    rate = count / elapsed if elapsed > 0 else 0
    print(f"  [W={W_bs}, B={B_bs}] DONE   "
          f"{count:,} boards in {format_duration(elapsed)} "
          f"({rate:,.0f} boards/s)",
          flush=True)
    return (W_bs, B_bs, temp_db, count, elapsed)

# ------------------------------------------------------------
# Merge all temp DBs into one
# ------------------------------------------------------------
def merge_temp_dbs(out_db, temp_dbs):
    print(f"\n=== PHASE: merge ({len(temp_dbs)} temp DBs) ===")
    t0 = time.perf_counter()

    if os.path.exists(out_db):
        os.remove(out_db)
    conn, cur = init_database(out_db)

    for tdb in temp_dbs:
        if not os.path.exists(tdb):
            continue
        t1 = time.perf_counter()
        sconn = sqlite3.connect(tdb)
        scur = sconn.cursor()
        scur.execute("SELECT piece_count, piece_type, board FROM boards")
        rows = scur.fetchall()
        cur.executemany(
            "INSERT OR IGNORE INTO boards (piece_count, piece_type, board) "
            "VALUES (?, ?, ?)", rows)
        conn.commit()
        sconn.close()
        print(f"    merged {tdb}: {len(rows):,} rows in "
              f"{format_duration(time.perf_counter() - t1)}", flush=True)

    cur.execute("SELECT COUNT(*) FROM boards")
    final_count = cur.fetchone()[0]
    conn.close()

    print(f"  merge done: {final_count:,} unique boards in "
          f"{format_duration(time.perf_counter() - t0)}", flush=True)
    return final_count

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4,
                        help='Number of parallel worker processes')
    parser.add_argument('--out', type=str, default='chess_boards_grouped.db',
                        help='Final merged database path')
    parser.add_argument('--keep-temp', action='store_true',
                        help='Do not delete per-worker temp databases')
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # GLOBAL TIMER
    # ------------------------------------------------------------------
    t_global = time.perf_counter()
    T = Timer()

    print("=" * 60)
    print("BOARD GENERATOR — NEW 4x4 VARIANT")
    print("=" * 60)
    print(f"Start time (local): {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Parallel workers  : {args.workers}")
    print(f"Output database   : {args.out}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # PHASE: setup
    # ------------------------------------------------------------------
    T.start('setup')
    weight = {0: 1, 1: 7, 2: 7, 3: 49}
    tasks = []
    for w in range(4):
        for b in range(4):
            tdb = f"chess_boards_state_W{w}_B{b}.db"
            if os.path.exists(tdb):
                os.remove(tdb)
            tasks.append((w, b, tdb))
    tasks.sort(key=lambda t: -(weight[t[0]] * weight[t[1]]))
    T.stop('setup')

    # Signal handler so Ctrl+C is graceful
    def _sigint(sig, frame):
        print("\nInterrupted. Terminating workers ...")
        sys.exit(1)
    signal.signal(signal.SIGINT, _sigint)

    # ------------------------------------------------------------------
    # PHASE: generation (parallel)
    # ------------------------------------------------------------------
    print("\n=== PHASE: generation (parallel) ===")
    T.start('generation')

    results = []
    with mp.Pool(processes=args.workers) as pool:
        for res in pool.imap_unordered(worker, tasks):
            results.append(res)
            done_boards  = sum(r[3] for r in results)
            done_seconds = sum(r[4] for r in results)
            print(f"  -> W={res[0]}, B={res[1]}: {res[3]:,} boards "
                  f"in {format_duration(res[4])} "
                  f"(running total {done_boards:,} boards, "
                  f"cumulative worker-time {format_duration(done_seconds)})",
                  flush=True)

    T.stop('generation')

    # ------------------------------------------------------------------
    # PHASE: merge
    # ------------------------------------------------------------------
    T.start('merge')
    temp_dbs = [r[2] for r in results]
    final_count = merge_temp_dbs(args.out, temp_dbs)
    T.stop('merge')

    # ------------------------------------------------------------------
    # PHASE: cleanup
    # ------------------------------------------------------------------
    T.start('cleanup')
    if not args.keep_temp:
        removed = 0
        for tdb in temp_dbs:
            try:
                os.remove(tdb)
                removed += 1
            except OSError:
                pass
        print(f"\n=== PHASE: cleanup ===")
        print(f"  removed {removed} temp DBs")
    T.stop('cleanup')

    # ------------------------------------------------------------------
    # PHASE: breakdown
    # ------------------------------------------------------------------
    print(f"\n=== PHASE: breakdown ===")
    T.start('breakdown')
    conn = sqlite3.connect(args.out)
    cur  = conn.cursor()
    cur.execute("""SELECT piece_count, piece_type, COUNT(*)
                   FROM boards
                   GROUP BY piece_count, piece_type
                   ORDER BY piece_count, piece_type""")
    rows = cur.fetchall()
    conn.close()
    T.stop('breakdown')

    # ------------------------------------------------------------------
    # FINAL REPORT
    # ------------------------------------------------------------------
    total_elapsed = time.perf_counter() - t_global

    print("\n" + "=" * 60)
    print("GENERATION COMPLETE")
    print("=" * 60)
    print(f"End time (local) : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total boards     : {final_count:,}")
    print(f"Expected         : 61,660,012")
    if final_count == 61_660_012:
        print("Result           : ✓ MATCH — board generator is correct")
    else:
        print(f"Result           : ✗ MISMATCH — difference "
              f"{final_count - 61_660_012:+,}")

    if total_elapsed > 0:
        print(f"Throughput       : {final_count / total_elapsed:,.0f} boards/s "
              f"(wall-clock, all workers combined)")

    # Per-task timings
    print("\nPer-task worker times (sorted by duration, descending):")
    for r in sorted(results, key=lambda x: -x[4]):
        print(f"  W={r[0]}, B={r[1]}: {r[3]:>12,} boards in "
              f"{format_duration(r[4])}")

    # Breakdown
    print("\nBreakdown by piece count and piece type:")
    for pc, pt, cnt in rows:
        print(f"  {pc} pieces, type {pt}: {cnt:,}")

    # Phase timing summary
    T.report(total_elapsed)

if __name__ == "__main__":
    main()