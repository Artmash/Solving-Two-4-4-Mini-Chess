#!/usr/bin/env python3
# apply_50_move_rule.py
#
# Correctly applies the 50-move rule to reachable_graph.db.
#
# Method:
#   * BFS from the start node, tracking moves_since_capture on each edge.
#   * A capture edge resets the counter to 0.
#   * A non-capture edge increments it by 1.
#   * The counter stored for each node is the MINIMUM value over all
#     BFS paths to it (i.e., the best case for the side to move).
#   * Any node whose MINIMUM counter reaches 100 is a 50-move draw.
#
# Why minimum? Under game rules, if ANY sequence of play reaches the
# node with >= 100 plies without a capture, the side to move can claim
# the draw. Using the min ensures we only mark nodes that are reachable
# with the short counter — which is the fair comparison.
#
# The script also reports the maximum counter reached, so the report
# can state whether the rule is ever triggered for this variant.
# ----------------------------------------------------------------------

import sqlite3
import time
import os
from collections import deque, defaultdict

DB_GRAPH = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"
THRESHOLD = 100     # plies without capture

# ------------------------------------------------------------
def format_duration(s):
    if s >= 3600:
        h = int(s // 3600); m = int((s % 3600) // 60); s -= h * 3600 + m * 60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if s >= 60:
        m = int(s // 60); s -= m * 60
        return f"{m}m {s:05.2f}s"
    return f"{s:.2f}s"

# ------------------------------------------------------------
def piece_count_of(board_str):
    return sum(1 for ch in board_str if ch not in ('.', ';'))

# ------------------------------------------------------------
def main():
    t0 = time.perf_counter()
    print("=" * 80)
    print("APPLY 50-MOVE RULE (no capture for 100 plies)")
    print("=" * 80)
    print(f"Start time (local) : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Graph DB           : {DB_GRAPH}")
    print(f"Threshold          : {THRESHOLD} plies")
    print("=" * 80)

    # ----------------------------------------------------------
    # 1. Load edges (parent -> child) and board piece counts
    # ----------------------------------------------------------
    print("\n=== PHASE: load graph ===")
    t = time.perf_counter()

    conn = sqlite3.connect(DB_GRAPH, timeout=180)
    cur  = conn.cursor()
    cur.execute("PRAGMA cache_size = -262144")   # 256 MB
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")

    # (node_id, board_id, side) for every node
    cur.execute("SELECT node_id, board_id, side FROM nodes")
    node_rows = cur.fetchall()
    n_nodes = len(node_rows)
    print(f"  {n_nodes:,} nodes loaded")

    node_board = {}   # node_id -> board_id
    node_side  = {}   # node_id -> 'w' / 'b'
    for nid, bid, side in node_rows:
        node_board[nid] = bid
        node_side[nid]  = side
    del node_rows

    # board piece counts
    cur.execute("SELECT id, piece_count FROM boards_db.boards")
    board_pc = dict(cur.fetchall())
    print(f"  {len(board_pc):,} board piece counts loaded")

    # Start node
    cur.execute("SELECT value FROM state WHERE key='start_node_id'")
    row = cur.fetchone()
    if not row:
        print("ERROR: no start_node_id in state table"); raise SystemExit(1)
    start_node = int(row[0])
    print(f"  start node: {start_node}")

    # ----------------------------------------------------------
    # 2. BFS with moves_since_capture tracking
    # ----------------------------------------------------------
    print("\n=== PHASE: BFS with moves_since_capture ===")
    t_bfs = time.perf_counter()

    # Fast parent -> children adjacency in memory
    # We only need child_node_id + whether edge is a capture.
    cur.execute("SELECT parent_node_id, child_node_id FROM edges")
    children_by_parent = defaultdict(list)
    n_edges = 0
    for p, c in cur:
        children_by_parent[p].append(c)
        n_edges += 1
    print(f"  {n_edges:,} edges loaded in {format_duration(time.perf_counter()-t)}")

    # Distances: minimal moves_since_capture per node
    INF = 10**9
    moves_since = {start_node: 0}
    q = deque([start_node])
    max_counter = 0
    n_reached = 0
    n_marked = 0
    nodes_to_mark = []

    while q:
        node = q.popleft()
        cur_counter = moves_since[node]
        n_reached += 1

        # If already over threshold, record but do NOT expand
        if cur_counter >= THRESHOLD:
            nodes_to_mark.append(node)
            n_marked += 1
            continue

        parent_board = node_board.get(node)
        parent_pc    = board_pc.get(parent_board, 0)

        for child in children_by_parent.get(node, ()):
            child_board = node_board.get(child)
            child_pc    = board_pc.get(child_board, 0)

            is_capture = (child_pc < parent_pc)
            new_counter = 0 if is_capture else cur_counter + 1

            # Only keep the minimum counter for each child
            if new_counter < moves_since.get(child, INF):
                moves_since[child] = new_counter
                if new_counter > max_counter:
                    max_counter = new_counter
                q.append(child)

        if n_reached % 500000 == 0:
            elapsed = time.perf_counter() - t_bfs
            print(f"    reached {n_reached:,} | "
                  f"queue {len(q):,} | "
                  f"max_counter {max_counter} | "
                  f"{format_duration(elapsed)}", flush=True)

    print(f"\n  BFS finished: {n_reached:,} nodes reached, "
          f"max moves_since_capture = {max_counter}")

    conn.close()

    # ----------------------------------------------------------
    # 3. Mark 50-move draws (if any)
    # ----------------------------------------------------------
    print("\n=== PHASE: mark 50-move draws ===")
    t_mark = time.perf_counter()

    if max_counter < THRESHOLD:
        print(f"  ✓ The 50-move rule NEVER triggers in this variant.")
        print(f"    (max counter reached: {max_counter}, threshold: {THRESHOLD})")
        print(f"    No nodes marked.")
    else:
        print(f"  Marking {n_marked:,} nodes with "
              f"moves_since_capture >= {THRESHOLD} as DRAW")

        conn = sqlite3.connect(DB_GRAPH)
        cur  = conn.cursor()
        cur.execute("PRAGMA synchronous = OFF")

        # Ensure column exists
        try:
            cur.execute("ALTER TABLE nodes ADD COLUMN draw_type_50 INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        BATCH = 5000
        for i in range(0, len(nodes_to_mark), BATCH):
            chunk = nodes_to_mark[i:i+BATCH]
            cur.executemany("""
                UPDATE nodes
                SET is_terminal     = 1,
                    value           = 'DRAW',
                    draw_type       = '50_move',
                    draw_type_50    = 1
                WHERE node_id = ?
            """, [(n,) for n in chunk])
            conn.commit()

        conn.close()
        print(f"  done in {format_duration(time.perf_counter()-t_mark)}")

    # ----------------------------------------------------------
    # Summary
    # ----------------------------------------------------------
    total_elapsed = time.perf_counter() - t0
    print("\n" + "=" * 80)
    print("50-MOVE RULE COMPLETE")
    print("=" * 80)
    print(f"End time (local)               : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Nodes visited by BFS           : {n_reached:,}")
    print(f"Max moves_since_capture        : {max_counter}")
    print(f"Threshold                      : {THRESHOLD}")
    print(f"Nodes marked as 50-move draws  : {n_marked:,}")
    print(f"Rule triggers in this variant? : "
          f"{'YES' if max_counter >= THRESHOLD else 'NO'}")
    print(f"Total time                     : {format_duration(total_elapsed)}")
    print("=" * 80)


if __name__ == "__main__":
    main()