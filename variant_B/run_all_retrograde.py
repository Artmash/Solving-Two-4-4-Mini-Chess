#!/usr/bin/env python3
# run_all_retrograde.py
#
# Runs retrograde layer-by-layer, bottom-up: 3 -> 4 -> 5 -> 6 -> 7 -> 8.
# Each layer is solved in its own subprocess so memory is fully reclaimed
# between layers.
#
# After layer 8, the runner inspects the start node's value and prints the
# game-theoretic outcome for the variant.
# ----------------------------------------------------------------------

import subprocess
import sys
import time
import sqlite3

LAYERS = [3, 4, 5, 6, 7, 8]

def format_duration(s):
    if s >= 3600:
        h = int(s // 3600); m = int((s % 3600) // 60); s -= h * 3600 + m * 60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if s >= 60:
        m = int(s // 60); s -= m * 60
        return f"{m}m {s:05.2f}s"
    return f"{s:.2f}s"


def main():
    overall = time.perf_counter()

    print("=" * 72)
    print("RETROGRADE — ALL LAYERS (BOTTOM-UP, 3 -> 8)")
    print("=" * 72)
    print(f"Start time (local): {time.strftime('%Y-%m-%d %H:%M:%S')}")

    layer_times = []

    for L in LAYERS:
        print(f"\n{'#' * 72}")
        print(f"# LAYER {L}")
        print(f"{'#' * 72}")
        t = time.perf_counter()
        r = subprocess.run(
            [sys.executable, "retrograde_layer.py", "--layer", str(L)])
        dur = time.perf_counter() - t
        layer_times.append((L, dur))
        print(f"\nLayer {L}: exit code {r.returncode}, "
              f"time {format_duration(dur)}")
        if r.returncode != 0:
            print(f"  !! Layer {L} failed — aborting.")
            sys.exit(r.returncode)

    # ------------------------------------------------------------------
    # Final verification
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("FINAL VERIFICATION")
    print("=" * 72)

    conn = sqlite3.connect("reachable_graph.db")
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*), "
                "SUM(value='WIN'), "
                "SUM(value='LOSS'), "
                "SUM(value='DRAW'), "
                "SUM(value IS NULL) "
                "FROM nodes")
    total, n_win, n_loss, n_draw, n_null = cur.fetchone()
    print(f"Nodes               : {total:,}")
    print(f"  WIN               : {n_win:,}")
    print(f"  LOSS              : {n_loss:,}")
    print(f"  DRAW              : {n_draw:,}")
    print(f"  NULL (unassigned) : {n_null:,}")

    # Value by piece count
    cur.execute("""
        SELECT b.piece_count, n.value, COUNT(*)
        FROM nodes n
        JOIN boards.boards b ON n.board_id = b.id
        WHERE 1=1
        GROUP BY b.piece_count, n.value
        ORDER BY b.piece_count, n.value
    """) if False else None
    conn.close()

    # Re-open with attached boards DB for the by-layer breakdown
    conn = sqlite3.connect("reachable_graph.db")
    cur  = conn.cursor()
    cur.execute("ATTACH DATABASE 'chess_boards_grouped.db' AS boards_db")
    cur.execute("""
        SELECT b.piece_count, n.value, COUNT(*)
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
        GROUP BY b.piece_count, n.value
        ORDER BY b.piece_count, n.value
    """)
    print("\nValue distribution by piece count:")
    current_pc = None
    for pc, v, c in cur.fetchall():
        if pc != current_pc:
            print(f"  --- {pc} pieces ---")
            current_pc = pc
        v_str = v if v is not None else 'NULL'
        print(f"    {v_str:<6s}: {c:,}")

    # Start node
    cur.execute("SELECT value FROM state WHERE key='start_node_id'")
    row = cur.fetchone()
    if row:
        start_nid = int(row[0])
        cur.execute("SELECT value FROM nodes WHERE node_id=?", (start_nid,))
        sv = cur.fetchone()[0]
        print("\n" + "*" * 72)
        print(f"  START NODE {start_nid}: {sv}")
        if sv == 'WIN':
            print("  *** WHITE HAS A WINNING STRATEGY ***")
        elif sv == 'LOSS':
            print("  *** WHITE LOSES UNDER PERFECT PLAY ***")
        else:
            print("  *** DRAW UNDER PERFECT PLAY ***")
        print("*" * 72)

    conn.close()

    # ------------------------------------------------------------------
    # Runner summary
    # ------------------------------------------------------------------
    total_elapsed = time.perf_counter() - overall
    print("\n" + "=" * 72)
    print("RUNNER TIMING SUMMARY")
    print("=" * 72)
    for L, dur in layer_times:
        print(f"  Layer {L}          : {format_duration(dur)}")
    print(f"  {'TOTAL':<17s}: {format_duration(total_elapsed)}")
    print("=" * 72)


if __name__ == "__main__":
    main()