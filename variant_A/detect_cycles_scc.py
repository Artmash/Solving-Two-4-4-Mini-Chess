#!/usr/bin/env python3
# detect_cycles_scc.py
# Tarjan's SCC via SciPy (C-implemented) — fast and memory-efficient.
# Each piece-count layer writes to its own database.
# Peak memory per worker: ~1-2 GB instead of ~8-9 GB.

import sqlite3
import time
import json
import os
import multiprocessing as mp
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from collections import defaultdict

# ------------------------------------------------------------
DB_GRAPH = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"
OUT_DB = "cycle_analysis_scc.db"

# Serial for the two largest layers, parallel for the rest.
NUM_WORKERS = 2        # reduced from 4 to keep peak RAM low
SERIAL_LAYERS = {7, 8} # these two run one at a time

# ------------------------------------------------------------
def process_layer(args):
    piece_count, node_ids = args
    t_start = time.time()

    worker_db = f"cycle_scc_layer_{piece_count}.db"
    if os.path.exists(worker_db):
        try:
            os.remove(worker_db)
        except PermissionError:
            pass

    print(f"[Layer {piece_count}] START — {len(node_ids):,} nodes", flush=True)

    # ------------------------------------------------------------
    # 1. Load edges for this layer into numpy arrays (streamed)
    # ------------------------------------------------------------
    conn = sqlite3.connect(DB_GRAPH, timeout=180)
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")

    # Temp table for the layer
    cur.execute("DROP TABLE IF EXISTS temp_layer")
    cur.execute("CREATE TEMP TABLE temp_layer (node_id INTEGER PRIMARY KEY)")
    BATCH = 10000
    for i in range(0, len(node_ids), BATCH):
        chunk = node_ids[i:i+BATCH]
        cur.executemany(
            "INSERT OR IGNORE INTO temp_layer (node_id) VALUES (?)",
            [(n,) for n in chunk])
    conn.commit()

    t = time.time()
    cur.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN temp_layer p ON e.parent_node_id = p.node_id
        JOIN temp_layer c ON e.child_node_id = c.node_id
    """)
    n_edges = cur.fetchone()[0]

    # Stream edges into numpy arrays
    parents = np.empty(n_edges, dtype=np.int64)
    children = np.empty(n_edges, dtype=np.int64)
    cur.execute("""
        SELECT e.parent_node_id, e.child_node_id
        FROM edges e
        JOIN temp_layer p ON e.parent_node_id = p.node_id
        JOIN temp_layer c ON e.child_node_id = c.node_id
    """)
    idx = 0
    for p, c in cur:
        parents[idx] = p
        children[idx] = c
        idx += 1

    print(f"[Layer {piece_count}] {n_edges:,} edges loaded in {time.time()-t:.1f}s",
          flush=True)

    # ------------------------------------------------------------
    # 2. Load sides for these nodes
    # ------------------------------------------------------------
    t = time.time()
    cur.execute("""
        SELECT n.node_id, n.side
        FROM nodes n
        JOIN temp_layer t ON n.node_id = t.node_id
    """)
    sides_dict = dict(cur.fetchall())
    cur.execute("DROP TABLE temp_layer")
    conn.commit()
    print(f"[Layer {piece_count}] sides loaded in {time.time()-t:.1f}s", flush=True)

    # ------------------------------------------------------------
    # 3. Dense remap of node IDs to [0, N)
    # ------------------------------------------------------------
    t = time.time()
    node_ids_arr = np.sort(np.array(node_ids, dtype=np.int64))
    N = len(node_ids_arr)

    # Convert raw IDs to dense indices
    p_dense = np.searchsorted(node_ids_arr, parents)
    c_dense = np.searchsorted(node_ids_arr, children)
    del parents, children

    # Sort edges by parent (stable, so ordering is deterministic)
    order = np.argsort(p_dense, kind='stable')
    p_dense = p_dense[order]
    c_dense = c_dense[order]
    del order
    print(f"[Layer {piece_count}] dense remap in {time.time()-t:.1f}s", flush=True)

    # ------------------------------------------------------------
    # 4. Build CSR indptr
    # ------------------------------------------------------------
    indptr = np.zeros(N + 1, dtype=np.int64)
    counts = np.bincount(p_dense, minlength=N)
    indptr[1:] = np.cumsum(counts)
    del p_dense, counts

    # ------------------------------------------------------------
    # 5. Build CSR sparse matrix and run SciPy SCC
    # ------------------------------------------------------------
    t = time.time()
    data = np.ones(len(c_dense), dtype=np.uint8)
    csr = csr_matrix((data, c_dense, indptr), shape=(N, N))
    del data
    print(f"[Layer {piece_count}] CSR built in {time.time()-t:.1f}s", flush=True)

    t = time.time()
    n_comp, labels = connected_components(csr, directed=True, connection='strong')
    print(f"[Layer {piece_count}] SciPy SCC: {n_comp:,} components "
          f"in {time.time()-t:.1f}s", flush=True)

    # ------------------------------------------------------------
    # 6. Group components by label, keep SCCs of size >= 2
    # ------------------------------------------------------------
    t = time.time()
    # Sort nodes by label for fast grouping
    label_order = np.argsort(labels, kind='stable')
    labels_sorted = labels[label_order]
    # Find boundaries
    unique_labels, first_idx = np.unique(labels_sorted, return_index=True)
    boundaries = np.append(first_idx, len(labels_sorted))

    n_with_cycles = 0
    n_with_white_exit = 0
    n_without_white_exit = 0
    rows_to_insert = []

    for k in range(len(unique_labels)):
        start = boundaries[k]
        end = boundaries[k + 1]
        scc_size = end - start
        if scc_size < 2:
            continue

        n_with_cycles += 1
        # Recover dense indices in this SCC
        dense_members = label_order[start:end]
        # Convert to raw node IDs
        raw_members = node_ids_arr[dense_members]
        scc_set = set(dense_members.tolist())

        # Exit nodes
        exit_nodes_raw = []
        white_exit_nodes_raw = []
        for d_idx in dense_members:
            for eidx in range(indptr[d_idx], indptr[d_idx + 1]):
                child_dense = c_dense[eidx]
                if child_dense not in scc_set:
                    raw_nid = int(node_ids_arr[d_idx])
                    exit_nodes_raw.append(raw_nid)
                    if sides_dict.get(raw_nid) == 'w':
                        white_exit_nodes_raw.append(raw_nid)
                    break

        has_white_exit = 1 if white_exit_nodes_raw else 0
        if has_white_exit:
            n_with_white_exit += 1
        else:
            n_without_white_exit += 1

        rows_to_insert.append((
            piece_count,
            int(scc_size),
            json.dumps([int(x) for x in raw_members]),
            json.dumps(exit_nodes_raw),
            len(white_exit_nodes_raw),
            has_white_exit,
        ))

    print(f"[Layer {piece_count}] analysis in {time.time()-t:.1f}s", flush=True)

    # ------------------------------------------------------------
    # 7. Write output DB
    # ------------------------------------------------------------
    conn_out = sqlite3.connect(worker_db)
    cur_out = conn_out.cursor()
    cur_out.execute("""
        CREATE TABLE IF NOT EXISTS sccs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            piece_count INTEGER,
            scc_size INTEGER,
            nodes_json TEXT,
            exit_nodes_json TEXT,
            white_exit_count INTEGER,
            has_white_exit INTEGER
        )
    """)
    cur_out.executemany("""
        INSERT INTO sccs (piece_count, scc_size, nodes_json, exit_nodes_json,
                          white_exit_count, has_white_exit)
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows_to_insert)
    conn_out.commit()
    conn_out.close()
    conn.close()

    total_time = time.time() - t_start
    print(f"[Layer {piece_count}] DONE in {total_time:.1f}s "
          f"({total_time/60:.1f} min)", flush=True)

    return {
        "piece_count": piece_count,
        "nodes": len(node_ids),
        "edges": n_edges,
        "total_sccs": int(n_comp),
        "sccs_with_cycles": n_with_cycles,
        "sccs_with_white_exit": n_with_white_exit,
        "sccs_without_white_exit": n_without_white_exit,
        "total_time": total_time,
        "worker_db": worker_db,
    }

# ------------------------------------------------------------
def main():
    overall = time.time()
    print("=" * 80)
    print("TARJAN SCC CYCLE DETECTION (SciPy, memory-efficient)")
    print("=" * 80)
    print(f"Workers: {NUM_WORKERS}  |  Serial layers: {SERIAL_LAYERS}")

    # Load nodes by piece count
    print("\nLoading nodes grouped by piece count...")
    conn = sqlite3.connect(DB_GRAPH)
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")
    cur.execute("""
        SELECT n.node_id, b.piece_count
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
    """)
    rows = cur.fetchall()
    conn.close()

    nodes_by_pc = defaultdict(list)
    for nid, pc in rows:
        nodes_by_pc[pc].append(nid)
    del rows

    print(f"  {len(nodes_by_pc)} layers:")
    for pc in sorted(nodes_by_pc):
        print(f"    {pc} pieces: {len(nodes_by_pc[pc]):,} nodes")

    # Build task list — largest first
    all_tasks = sorted(nodes_by_pc.items(), key=lambda kv: -len(kv[1]))

    # Split into serial and parallel
    serial_tasks = [(pc, nids) for pc, nids in all_tasks if pc in SERIAL_LAYERS]
    parallel_tasks = [(pc, nids) for pc, nids in all_tasks if pc not in SERIAL_LAYERS]

    summaries = []

    # Run parallel layers first (smaller ones)
    print(f"\nRunning {len(parallel_tasks)} small layers in parallel...")
    if parallel_tasks:
        with mp.Pool(processes=NUM_WORKERS) as pool:
            for summary in pool.imap_unordered(process_layer, parallel_tasks):
                summaries.append(summary)
                print(f"  ✓ Layer {summary['piece_count']}: "
                      f"{summary['sccs_with_cycles']:,} cycles "
                      f"({summary['total_time']:.1f}s)")

    # Run large layers serially
    for task in serial_tasks:
        print(f"\nRunning large layer {task[0]} serially...")
        summary = process_layer(task)
        summaries.append(summary)
        print(f"  ✓ Layer {summary['piece_count']}: "
              f"{summary['sccs_with_cycles']:,} cycles "
              f"({summary['total_time']:.1f}s)")

    # ------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------
    print("\nMerging worker databases...")
    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)
    conn_out = sqlite3.connect(OUT_DB)
    cur_out = conn_out.cursor()
    cur_out.execute("""
        CREATE TABLE sccs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            piece_count INTEGER,
            scc_size INTEGER,
            nodes_json TEXT,
            exit_nodes_json TEXT,
            white_exit_count INTEGER,
            has_white_exit INTEGER
        )
    """)
    cur_out.execute("""
        CREATE TABLE layer_summary (
            piece_count INTEGER PRIMARY KEY,
            total_sccs INTEGER,
            sccs_with_cycles INTEGER,
            sccs_with_white_exit INTEGER,
            sccs_without_white_exit INTEGER,
            total_time REAL
        )
    """)
    conn_out.commit()

    total_cycles = total_with = total_without = 0
    for s in summaries:
        wdb = s["worker_db"]
        if not os.path.exists(wdb):
            continue
        conn_w = sqlite3.connect(wdb)
        cur_w = conn_w.cursor()
        cur_w.execute("""
            SELECT piece_count, scc_size, nodes_json, exit_nodes_json,
                   white_exit_count, has_white_exit
            FROM sccs
        """)
        cur_out.executemany("""
            INSERT INTO sccs (piece_count, scc_size, nodes_json, exit_nodes_json,
                              white_exit_count, has_white_exit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, cur_w.fetchall())
        conn_w.close()

        cur_out.execute("""
            INSERT INTO layer_summary VALUES (?, ?, ?, ?, ?, ?)
        """, (s["piece_count"], s["total_sccs"], s["sccs_with_cycles"],
              s["sccs_with_white_exit"], s["sccs_without_white_exit"],
              s["total_time"]))

        total_cycles += s["sccs_with_cycles"]
        total_with += s["sccs_with_white_exit"]
        total_without += s["sccs_without_white_exit"]

    conn_out.commit()
    conn_out.close()

    # Cleanup
    for s in summaries:
        wdb = s["worker_db"]
        if os.path.exists(wdb):
            try:
                os.remove(wdb)
            except PermissionError:
                pass

    print("\n" + "=" * 80)
    print("CYCLE ANALYSIS SUMMARY")
    print("=" * 80)
    print(f"Total SCCs with cycles: {total_cycles:,}")
    print(f"  With White exit:     {total_with:,}")
    print(f"  Without White exit:  {total_without:,}")

    if total_without > 0:
        print(f"\n⚠️  {total_without} SCC(s) have NO White exit.")
    else:
        print("\n✅ All SCCs have a White exit.")

    print(f"\nResults saved to {OUT_DB}")
    print(f"Total time: {time.time()-overall:.1f}s "
          f"({(time.time()-overall)/60:.1f} min)")

if __name__ == "__main__":
    main()