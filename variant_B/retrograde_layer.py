#!/usr/bin/env python3
# retrograde_layer.py
#
# Retrograde analysis for ONE piece-count layer.
# Layers must be run bottom-up: 3, 4, 5, 6, 7, 8.
#
# Usage:  python retrograde_layer.py --layer 5
# ----------------------------------------------------------------------

import sqlite3
import time
import argparse
from collections import deque, defaultdict

DB_GRAPH  = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"

FLUSH_BATCH  = 20000
LOG_INTERVAL = 15

# ----------------------------------------------------------------------
# Timer / formatting
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
# Core solver
# ----------------------------------------------------------------------
def solve_layer(L):
    t_global = time.perf_counter()
    T = Timer()

    print("=" * 72)
    print(f"RETROGRADE — LAYER {L}")
    print("=" * 72)
    print(f"Start time (local): {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------
    T.start('connect')
    conn = sqlite3.connect(DB_GRAPH, timeout=180)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA temp_store   = MEMORY")
    conn.execute("PRAGMA cache_size   = -262144")
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")

    for col, typ in [('value', 'TEXT'),
                     ('win_children', 'INTEGER DEFAULT 0')]:
        try:
            cur.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    T.stop('connect')

    # ------------------------------------------------------------------
    # Step 1 — Load layer nodes, remember terminal values
    # ------------------------------------------------------------------
    print(f"\n[1/7] Loading layer {L} ...")
    T.start('load')
    t = time.perf_counter()
    cur.execute("""
        SELECT n.node_id, n.side, n.total_children, n.value
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
        WHERE b.piece_count = ?
    """, (L,))
    rows = cur.fetchall()
    N = len(rows)
    if N == 0:
        print("  Layer is empty. Nothing to do.")
        conn.close()
        return

    total_children = {}
    side_of        = {}
    terminal_value = {}
    for nid, side, tc, val in rows:
        total_children[nid] = tc
        side_of[nid]        = side
        if val is not None:
            terminal_value[nid] = val

    n_term = len(terminal_value)
    print(f"  {N:,} nodes ({n_term:,} terminals) "
          f"loaded in {format_duration(time.perf_counter() - t)}")
    T.stop('load')

    # ------------------------------------------------------------------
    # Step 2 — Reset values in this layer
    # ------------------------------------------------------------------
    print(f"\n[2/7] Resetting values for layer {L} ...")
    T.start('reset')
    t = time.perf_counter()
    cur.execute("""
        UPDATE nodes
        SET value = NULL, win_children = 0
        WHERE node_id IN (
            SELECT n.node_id FROM nodes n
            JOIN boards_db.boards b ON n.board_id = b.id
            WHERE b.piece_count = ?
        )
    """, (L,))
    conn.commit()
    print(f"  Reset in {format_duration(time.perf_counter() - t)}")
    T.stop('reset')

    # ------------------------------------------------------------------
    # Step 3 — Initialise terminals
    # ------------------------------------------------------------------
    print(f"\n[3/7] Seeding terminals ...")
    T.start('seed_terminals')
    t = time.perf_counter()

    cur_values = dict(terminal_value)
    cur_wc     = {nid: 0 for nid in terminal_value}
    queue      = deque()

    n_seed_loss = 0
    for nid, v in terminal_value.items():
        if v == 'LOSS':
            queue.append(nid)
            n_seed_loss += 1

    print(f"  {n_term:,} terminals seeded, "
          f"{n_seed_loss:,} LOSS queued in "
          f"{format_duration(time.perf_counter() - t)}")
    T.stop('seed_terminals')

    # ------------------------------------------------------------------
    # Step 4 — Precompute win_children from LOWER layers
    # ------------------------------------------------------------------
    print(f"\n[4/7] Precomputing win_children from lower layers ...")
    T.start('precompute')
    t = time.perf_counter()

    cur.execute("""
        SELECT e.parent_node_id, n.value
        FROM edges e
        JOIN nodes n ON e.child_node_id = n.node_id
        WHERE e.parent_node_id IN (
            SELECT n2.node_id FROM nodes n2
            JOIN boards_db.boards b ON n2.board_id = b.id
            WHERE b.piece_count = ?
        )
        AND n.value IS NOT NULL
    """, (L,))

    has_loss_child = set()
    for p, v in cur:
        if v == 'WIN':
            cur_wc[p] = cur_wc.get(p, 0) + 1
        elif v == 'LOSS':
            has_loss_child.add(p)

    n_immediate_win  = 0
    n_immediate_loss = 0
    for nid in total_children:
        if nid in cur_values:
            continue
        wc = cur_wc.get(nid, 0)
        tc = total_children[nid]
        if nid in has_loss_child:
            cur_values[nid] = 'WIN'
            queue.append(nid)
            n_immediate_win += 1
        elif tc > 0 and wc == tc:
            cur_values[nid] = 'LOSS'
            queue.append(nid)
            n_immediate_loss += 1

    print(f"  immediate WIN from lower-layer LOSS: {n_immediate_win:,}")
    print(f"  immediate LOSS (all children WIN):  {n_immediate_loss:,}")
    print(f"  done in {format_duration(time.perf_counter() - t)}, "
          f"queue {len(queue):,}")
    T.stop('precompute')

    # ------------------------------------------------------------------
    # Step 5 — Reverse adjacency for SAME-LAYER edges
    # ------------------------------------------------------------------
    print(f"\n[5/7] Building same-layer reverse map ...")
    T.start('reverse')
    t = time.perf_counter()

    cur.execute("""
        SELECT e.child_node_id, e.parent_node_id
        FROM edges e
        WHERE e.parent_node_id IN (
            SELECT n.node_id FROM nodes n
            JOIN boards_db.boards b ON n.board_id = b.id
            WHERE b.piece_count = ?
        )
    """, (L,))

    reverse = defaultdict(list)
    n_same_edges = 0
    for c, p in cur:
        if c in total_children:
            reverse[c].append(p)
            n_same_edges += 1

    print(f"  {n_same_edges:,} same-layer edges "
          f"({len(reverse):,} reverse entries) in "
          f"{format_duration(time.perf_counter() - t)}")
    T.stop('reverse')

    # ------------------------------------------------------------------
    # Step 6 — Propagation
    # ------------------------------------------------------------------
    print(f"\n[6/7] Propagating ...")
    T.start('propagate')
    t = time.perf_counter()

    n_processed = 0
    last_log = time.perf_counter()

    while queue:
        nid = queue.popleft()
        val = cur_values[nid]
        parents = reverse.get(nid)
        if not parents:
            n_processed += 1
            continue

        for parent in parents:
            if parent in cur_values:
                continue
            wc = cur_wc.get(parent, 0)
            if val == 'LOSS':
                cur_values[parent] = 'WIN'
                queue.append(parent)
            elif val == 'WIN':
                wc += 1
                tc = total_children[parent]
                if tc > 0 and wc == tc:
                    cur_values[parent] = 'LOSS'
                cur_wc[parent] = wc
                if parent in cur_values:
                    queue.append(parent)

        n_processed += 1

        now = time.perf_counter()
        if (now - last_log) > LOG_INTERVAL:
            elapsed = now - t
            rate = n_processed / elapsed if elapsed > 0 else 0
            print(f"    processed {n_processed:>12,} | "
                  f"queue {len(queue):>10,} | "
                  f"{format_duration(elapsed):>12s} | "
                  f"{rate:>8,.0f} nodes/s", flush=True)
            last_log = now

    print(f"  done in {format_duration(time.perf_counter() - t)} "
          f"({n_processed:,} nodes processed)")
    T.stop('propagate')

    # ------------------------------------------------------------------
    # Step 7 — Flush values to DB
    # ------------------------------------------------------------------
    print(f"\n[7/7] Flushing to DB ...")
    T.start('flush')
    t = time.perf_counter()

    # Every node in cur_values gets written (WIN / LOSS / DRAW).
    # Nodes NOT in cur_values are marked DRAW.
    explicit_updates = [
        (v, cur_wc.get(nid, 0), nid)
        for nid, v in cur_values.items()
    ]

    assigned = set(cur_values)
    implicit_draws = [nid for nid in total_children if nid not in assigned]

    n_win  = sum(1 for v, _, _ in explicit_updates if v == 'WIN')
    n_loss = sum(1 for v, _, _ in explicit_updates if v == 'LOSS')
    n_draw_explicit = sum(1 for v, _, _ in explicit_updates if v == 'DRAW')
    n_draw_implicit = len(implicit_draws)

    # Explicit WIN / LOSS / DRAW updates
    for i in range(0, len(explicit_updates), FLUSH_BATCH):
        cur.executemany(
            "UPDATE nodes SET value = ?, win_children = ? WHERE node_id = ?",
            explicit_updates[i:i+FLUSH_BATCH])
    conn.commit()

    # Remaining NULLs become DRAW
    for i in range(0, len(implicit_draws), FLUSH_BATCH):
        cur.executemany(
            "UPDATE nodes SET value = 'DRAW', win_children = 0 WHERE node_id = ?",
            [(n,) for n in implicit_draws[i:i+FLUSH_BATCH]])
    conn.commit()

    print(f"  flushed WIN={n_win:,}, LOSS={n_loss:,}, "
          f"DRAW={n_draw_explicit + n_draw_implicit:,} "
          f"({n_draw_explicit:,} explicit + {n_draw_implicit:,} implicit) "
          f"in {format_duration(time.perf_counter() - t)}")
    T.stop('flush')

    # ------------------------------------------------------------------
    # Summary  (now safe: no NULLs left in this layer)
    # ------------------------------------------------------------------
    print(f"\n[Summary] Layer {L} value distribution:")
    cur.execute("""
        SELECT COALESCE(value, 'NULL') AS v, COUNT(*)
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
        WHERE b.piece_count = ?
        GROUP BY v
        ORDER BY v
    """, (L,))
    for v, c in cur.fetchall():
        print(f"    {str(v):<6s}: {c:,}")

    if L == 8:
        cur.execute("SELECT value FROM state WHERE key = 'start_node_id'")
        row = cur.fetchone()
        if row:
            start_nid = int(row[0])
            cur.execute("SELECT value FROM nodes WHERE node_id = ?", (start_nid,))
            sv_row = cur.fetchone()
            sv = sv_row[0] if sv_row else None
            print("\n" + "*" * 72)
            print(f"  *** START NODE VALUE: {sv} ***")
            if sv == 'WIN':
                print("  *** White has a winning strategy! ***")
            elif sv == 'LOSS':
                print("  *** White loses with perfect play. ***")
            else:
                print("  *** DRAW — White cannot force a win. ***")
            print("*" * 72)

    cur.execute("DETACH DATABASE boards_db")
    conn.close()

    total = time.perf_counter() - t_global
    print(f"\nLayer {L} total time: {format_duration(total)}")
    T.report(total)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--layer', type=int, required=True)
    args = ap.parse_args()
    solve_layer(args.layer)