#!/usr/bin/env python3
# 50_move_rule.py
# Marks nodes that are draws due to the 50-move rule.
# Uses the SCC output from detect_cycles_scc.py.
# Fixes the "too many SQL variables" error by using a temp table.

import sqlite3
import time
import json

DB_GRAPH = "reachable_graph.db"
DB_SCC = "cycle_analysis_scc.db"

def main():
    t0 = time.time()
    print("=" * 70)
    print("APPLY 50-MOVE RULE MARKING")
    print("=" * 70)

    conn_g = sqlite3.connect(DB_GRAPH)
    cur_g = conn_g.cursor()

    # Ensure column exists
    try:
        cur_g.execute("ALTER TABLE nodes ADD COLUMN draw_type_50 INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn_g.commit()

    # Load SCCs
    conn_s = sqlite3.connect(DB_SCC)
    cur_s = conn_s.cursor()
    cur_s.execute("SELECT id, piece_count, nodes_json FROM sccs")
    sccs = cur_s.fetchall()
    conn_s.close()

    print(f"Loaded {len(sccs)} SCCs")

    marked_total = 0
    sccs_with_capture = 0
    sccs_without_capture = 0

    for scc_id, piece_count, nodes_json in sccs:
        scc_nodes = json.loads(nodes_json)
        scc_size = len(scc_nodes)

        # ------------------------------------------------------------
        # Use a temp table instead of an IN clause — avoids SQLite's
        # ~999 parameter limit for large SCCs.
        # ------------------------------------------------------------
        cur_g.execute("DROP TABLE IF EXISTS temp_scc")
        cur_g.execute("CREATE TEMP TABLE temp_scc (node_id INTEGER PRIMARY KEY)")

        # Insert SCC nodes in batches
        BATCH = 5000
        for i in range(0, scc_size, BATCH):
            chunk = scc_nodes[i:i+BATCH]
            cur_g.executemany(
                "INSERT OR IGNORE INTO temp_scc (node_id) VALUES (?)",
                [(n,) for n in chunk]
            )
        conn_g.commit()

        # Check: does any node in this SCC have an outgoing edge
        # to a node OUTSIDE the SCC?
        # Since SCCs are computed per piece-count layer, any such edge
        # must be a capture (piece count decreases).
        cur_g.execute("""
            SELECT 1 FROM edges e
            WHERE e.parent_node_id IN (SELECT node_id FROM temp_scc)
              AND e.child_node_id NOT IN (SELECT node_id FROM temp_scc)
            LIMIT 1
        """)
        has_capture = cur_g.fetchone() is not None

        cur_g.execute("DROP TABLE temp_scc")
        conn_g.commit()

        if has_capture:
            sccs_with_capture += 1
        else:
            sccs_without_capture += 1
            # All nodes in this SCC are 50-move-rule draws
            cur_g.executemany(
                "UPDATE nodes SET draw_type_50 = 1 WHERE node_id = ?",
                [(n,) for n in scc_nodes]
            )
            marked_total += scc_size
            conn_g.commit()

        print(f"  SCC {scc_id} ({piece_count} pieces, {scc_size} nodes): "
              f"{'has capture' if has_capture else 'NO capture -> 50-move draw'}")

    # ------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  SCCs with capture exit:     {sccs_with_capture}")
    print(f"  SCCs without capture exit:  {sccs_without_capture}")
    print(f"  Nodes marked as 50-move:    {marked_total:,}")
    print(f"  Time: {time.time()-t0:.1f}s")

    conn_g.close()


if __name__ == "__main__":
    main()