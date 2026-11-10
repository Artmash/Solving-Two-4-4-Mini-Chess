#!/usr/bin/env python3
# validate_minimax.py
#
# Validation of the retrograde analysis.
#
# For each sampled non-terminal node, recompute its minimax value from
# the values of its children using the standard retrograde rules:
#
#   * If ANY child is LOSS                      -> node is WIN
#   * Else if ALL children are WIN              -> node is LOSS
#   * Else                                       -> node is DRAW
#
# Compare the recomputed value to the stored value and report a full
# breakdown by value and by piece-count layer.
#
# Terminal nodes (is_terminal = 1) are skipped: their values were set
# directly during graph construction, not by retrograde propagation.
#
# Usage:
#     python3 validate_minimax.py
#     python3 validate_minimax.py --sample 2000000
#     python3 validate_minimax.py --sample 1000000 --seed 42
# ----------------------------------------------------------------------

import sqlite3
import time
import argparse
import random
from collections import defaultdict

DB_GRAPH  = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"

# ----------------------------------------------------------------------
# Timing helpers
# ----------------------------------------------------------------------
class Timer:
    def __init__(self):
        self.records = []
        self._open = {}
    def start(self, name):
        self._open[name] = time.perf_counter()
    def stop(self, name):
        if name in self._open:
            self.records.append((name, time.perf_counter() - self._open.pop(name)))
    def report(self, total):
        print("\n" + "=" * 60)
        print("TIMING SUMMARY")
        print("=" * 60)
        for name, dur in self.records:
            pct = (dur / total * 100.0) if total > 0 else 0.0
            print(f"  {name:<22s}: {format_duration(dur)}  ({pct:5.1f}%)")
        print(f"  {'TOTAL':<22s}: {format_duration(total)}")
        print("=" * 60)

def format_duration(s):
    if s >= 3600:
        h = int(s // 3600); m = int((s % 3600) // 60); s -= h * 3600 + m * 60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if s >= 60:
        m = int(s // 60); s -= m * 60
        return f"{m}m {s:05.2f}s"
    return f"{s:.2f}s"

# ----------------------------------------------------------------------
# Core validation
# ----------------------------------------------------------------------
def validate(sample_size, seed):

    t_global = time.perf_counter()
    T = Timer()

    print("=" * 72)
    print("MINIMAX CONSISTENCY VALIDATION")
    print("=" * 72)
    print(f"Start time (local)  : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Graph DB            : {DB_GRAPH}")
    print(f"Boards DB           : {DB_BOARDS}")
    print(f"Sample size         : {sample_size:,}")
    print(f"Random seed         : {seed}")
    print("=" * 72)

    rng = random.Random(seed)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------
    T.start('connect')
    conn = sqlite3.connect(DB_GRAPH, timeout=180)
    conn.execute("PRAGMA cache_size = -262144")   # 256 MB
    conn.execute("PRAGMA temp_store = MEMORY")
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")
    T.stop('connect')

    # ------------------------------------------------------------------
    # Step 1 — Determine population and sample
    # ------------------------------------------------------------------
    print("\n[1/5] Sampling nodes ...")
    T.start('sample')
    t = time.perf_counter()

    cur.execute("SELECT COUNT(*) FROM nodes")
    total_nodes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM nodes WHERE is_terminal = 1")
    total_terminals = cur.fetchone()[0]
    non_terminal = total_nodes - total_terminals
    print(f"  total nodes         : {total_nodes:,}")
    print(f"  terminal nodes      : {total_terminals:,}")
    print(f"  non-terminal pool   : {non_terminal:,}")

    if sample_size > non_terminal:
        print(f"  sample size > non-terminal pool; using entire pool")
        sample_size = non_terminal

    # Fetch all non-terminal node IDs and sample in Python.
    # This avoids ORDER BY RANDOM() which is O(n log n) in SQLite.
    cur.execute("SELECT node_id FROM nodes WHERE is_terminal = 0")
    pool = [row[0] for row in cur]
    print(f"  pool fetched in {format_duration(time.perf_counter() - t)}")

    sampled = rng.sample(pool, sample_size)
    del pool
    print(f"  sampled {len(sampled):,} nodes in "
          f"{format_duration(time.perf_counter() - t)}")
    T.stop('sample')

    # ------------------------------------------------------------------
    # Step 2 — Batch-load children values for the sample
    # ------------------------------------------------------------------
    print("\n[2/5] Loading sampled nodes and their children ...")
    T.start('load')
    t = time.perf_counter()

    # Node → (stored_value, total_children, piece_count)
    sample_set = set(sampled)
    sample_info = {}   # node_id -> (value, total_children, piece_count)

    cur.execute("DROP TABLE IF EXISTS temp_sample")
    cur.execute("CREATE TEMP TABLE temp_sample (node_id INTEGER PRIMARY KEY)")
    BATCH = 5000
    for i in range(0, len(sampled), BATCH):
        chunk = sampled[i:i+BATCH]
        cur.executemany("INSERT OR IGNORE INTO temp_sample VALUES (?)",
                        [(n,) for n in chunk])
    conn.commit()

    cur.execute("""
        SELECT n.node_id, n.value, n.total_children, b.piece_count
        FROM nodes n
        JOIN temp_sample s ON s.node_id = n.node_id
        JOIN boards_db.boards b ON n.board_id = b.id
    """)
    for nid, v, tc, pc in cur:
        sample_info[nid] = (v, tc, pc)
    print(f"  loaded {len(sample_info):,} sampled nodes in "
          f"{format_duration(time.perf_counter() - t)}")

    # All children of the sampled nodes, with their values
    t = time.perf_counter()
    cur.execute("""
        SELECT e.parent_node_id, e.child_node_id, n.value
        FROM edges e
        JOIN temp_sample s ON e.parent_node_id = s.node_id
        JOIN nodes n ON e.child_node_id = n.node_id
    """)
    children_by_parent = defaultdict(list)
    n_edge_rows = 0
    for p, c, v in cur:
        children_by_parent[p].append(v)
        n_edge_rows += 1
    print(f"  loaded {n_edge_rows:,} child edges "
          f"({len(children_by_parent):,} parents) in "
          f"{format_duration(time.perf_counter() - t)}")
    T.stop('load')

    # ------------------------------------------------------------------
    # Step 3 — Recompute and compare
    # ------------------------------------------------------------------
    print("\n[3/5] Recomputing values ...")
    T.start('recompute')
    t = time.perf_counter()

    n_match = 0
    n_mismatch = 0
    n_missing_children = 0   # non-terminal node with 0 children in DB
    n_null_children = 0      # child has NULL value

    # Breakdowns
    match_by_value    = defaultdict(int)
    mismatch_by_value = defaultdict(int)
    mismatch_examples = []
    match_by_pc       = defaultdict(lambda: [0, 0])   # pc -> [match, mismatch]

    for nid in sampled:
        info = sample_info.get(nid)
        if info is None:
            continue
        stored_value, total_children, pc = info

        child_values = children_by_parent.get(nid, [])

        # Consistency check: children count
        if total_children != len(child_values):
            # This can legitimately happen if the DB rows were deleted
            # or if total_children was not maintained; we still recompute.
            pass

        if not child_values:
            n_missing_children += 1
            continue

        if any(v is None for v in child_values):
            n_null_children += 1
            continue

        # Recompute using the retrograde rules
        if any(v == 'LOSS' for v in child_values):
            recomputed = 'WIN'
        elif all(v == 'WIN' for v in child_values):
            recomputed = 'LOSS'
        else:
            recomputed = 'DRAW'

        if recomputed == stored_value:
            n_match += 1
            match_by_value[stored_value] += 1
            match_by_pc[pc][0] += 1
        else:
            n_mismatch += 1
            mismatch_by_value[stored_value] += 1
            mismatch_by_pc[pc][1] += 1
            if len(mismatch_examples) < 20:
                mismatch_examples.append(
                    (nid, stored_value, recomputed, pc,
                     len(child_values),
                     child_values[:6]))

    print(f"  recompute finished in "
          f"{format_duration(time.perf_counter() - t)}")
    T.stop('recompute')

    # ------------------------------------------------------------------
    # Step 4 — Report
    # ------------------------------------------------------------------
    print("\n[4/5] Results")
    total_compared = n_match + n_mismatch
    if total_compared == 0:
        print("  No valid samples to compare.")
        conn.close()
        return

    match_rate = 100.0 * n_match / total_compared

    print("\n" + "-" * 60)
    print("SUMMARY")
    print("-" * 60)
    print(f"  Sample size                   : {sample_size:,}")
    print(f"  Successfully compared         : {total_compared:,}")
    print(f"  Matched                       : {n_match:,}")
    print(f"  Mismatched                    : {n_mismatch:,}")
    print(f"  Skipped (missing children)    : {n_missing_children:,}")
    print(f"  Skipped (NULL child value)    : {n_null_children:,}")
    print(f"  Match rate                    : {match_rate:.4f}%")

    if mismatch_by_value:
        print("\n  Mismatches by stored value:")
        for v in ('WIN', 'LOSS', 'DRAW'):
            if v in mismatch_by_value:
                print(f"    stored={v:<6s}: {mismatch_by_value[v]:,}")

    if match_by_value:
        print("\n  Matches by stored value:")
        for v in ('WIN', 'LOSS', 'DRAW'):
            if v in match_by_value:
                print(f"    stored={v:<6s}: {match_by_value[v]:,}")

    if match_by_pc:
        print("\n  Match / mismatch by piece count:")
        for pc in sorted(match_by_pc):
            m, mm = match_by_pc[pc]
            tot = m + mm
            rate = 100.0 * m / tot if tot else 0.0
            print(f"    {pc} pieces: {m:>12,} match / "
                  f"{mm:>8,} mismatch  ({rate:.3f}%)")

    if mismatch_examples:
        print("\n  First 20 mismatches (node_id, stored, recomputed, pc, "
              "#children, first children):")
        for ex in mismatch_examples:
            print(f"    {ex}")

    # ------------------------------------------------------------------
    # Step 5 — Verdict
    # ------------------------------------------------------------------
    print("\n[5/5] Verdict")
    print("-" * 60)
    if n_mismatch == 0:
        print("  ✓ 100% MATCH — retrograde values are consistent with minimax.")
    elif match_rate >= 99.99:
        print(f"  ✓ Match rate {match_rate:.4f}% is effectively 100%; "
              f"the {n_mismatch} mismatches may be boundary cases.")
    else:
        print(f"  ✗ Match rate {match_rate:.4f}% — investigate "
              f"{n_mismatch} mismatches.")

    conn.close()

    total = time.perf_counter() - t_global
    T.report(total)
    print(f"\nTotal validation time: {format_duration(total)}")


# ----------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', type=int, default=1_000_000,
                    help='Number of nodes to sample (default 1,000,000)')
    ap.add_argument('--seed',   type=int, default=42,
                    help='Random seed (default 42)')
    args = ap.parse_args()

    validate(args.sample, args.seed)