#!/usr/bin/env python3
# detect_cycles_scc.py
#
# Tarjan SCC per piece-count layer (SciPy C implementation).
# For each SCC of size >= 2, classifies it as:
#     CLOSED          no outgoing edges at all   -> terminal DRAW
#     WHITE_ESCAPE    White can leave
#     BLACK_ESCAPE    Black can leave
#     BOTH_ESCAPE     both sides can leave
#
# CLOSED SCCs are marked in reachable_graph.db as terminal DRAWs:
#     is_terminal = 1, draw_type = 'cycle_closed', value = 'DRAW'
#
# Open SCCs are NOT marked; their values will be determined by retrograde.
#
# Uses the same Timer/format_duration convention as the rest of the pipeline.
# ----------------------------------------------------------------------

import sqlite3
import time
import json
import os
import multiprocessing as mp
from collections import defaultdict

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
DB_GRAPH = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"
OUT_DB = "cycle_analysis_scc.db"

NUM_WORKERS = 2               # keep peak RAM low
SERIAL_LAYERS = {7, 8}        # largest layers run one at a time

# ------------------------------------------------------------
# Timing helpers
# ------------------------------------------------------------
class Timer:
    def __init__(self):
        self.records = []
        self._open = {}
    def start(self, name):
        self._open[name] = time.perf_counter()
    def stop(self, name):
        if name in self._open:
            self.records.append((name, time.perf_counter() - self._open.pop(name)))
    def report(self, total, header="TIMING SUMMARY"):
        print("\n" + "=" * 60)
        print(header)
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

# ------------------------------------------------------------
# Worker: process one piece-count layer
# ------------------------------------------------------------
def process_layer(args):
    piece_count, node_ids = args
    t_start = time.perf_counter()

    worker_db = f"cycle_scc_layer_{piece_count}.db"
    if os.path.exists(worker_db):
        try:
            os.remove(worker_db)
        except PermissionError:
            pass

    print(f"[Layer {piece_count}] START — {len(node_ids):,} nodes", flush=True)

    # ----------------------------------------------------------
    # 1. Load edges with both endpoints in this layer
    # ----------------------------------------------------------
    conn = sqlite3.connect(DB_GRAPH, timeout=180)
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")

    cur.execute("DROP TABLE IF EXISTS temp_layer")
    cur.execute("CREATE TEMP TABLE temp_layer (node_id INTEGER PRIMARY KEY)")
    BATCH = 10000
    for i in range(0, len(node_ids), BATCH):
        chunk = node_ids[i:i+BATCH]
        cur.executemany(
            "INSERT OR IGNORE INTO temp_layer (node_id) VALUES (?)",
            [(n,) for n in chunk])
    conn.commit()

    t = time.perf_counter()
    cur.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN temp_layer p ON e.parent_node_id = p.node_id
        JOIN temp_layer c ON e.child_node_id  = c.node_id
    """)
    n_edges = cur.fetchone()[0]
    print(f"[Layer {piece_count}] {n_edges:,} edges in layer", flush=True)

    parents  = np.empty(n_edges, dtype=np.int64)
    children = np.empty(n_edges, dtype=np.int64)

    cur.execute("""
        SELECT e.parent_node_id, e.child_node_id
        FROM edges e
        JOIN temp_layer p ON e.parent_node_id = p.node_id
        JOIN temp_layer c ON e.child_node_id  = c.node_id
    """)
    idx = 0
    for p, c in cur:
        parents[idx]  = p
        children[idx] = c
        idx += 1
    print(f"[Layer {piece_count}] edges loaded in "
          f"{format_duration(time.perf_counter()-t)}", flush=True)

    # ----------------------------------------------------------
    # 2. Load sides for these nodes
    # ----------------------------------------------------------
    t = time.perf_counter()
    cur.execute("""
        SELECT n.node_id, n.side
        FROM nodes n
        JOIN temp_layer t ON n.node_id = t.node_id
    """)
    sides_dict = dict(cur.fetchall())
    cur.execute("DROP TABLE temp_layer")
    conn.commit()
    print(f"[Layer {piece_count}] sides loaded in "
          f"{format_duration(time.perf_counter()-t)}", flush=True)

    # ----------------------------------------------------------
    # 3. Dense remap of raw node IDs -> [0, N)
    # ----------------------------------------------------------
    t = time.perf_counter()
    node_ids_arr = np.sort(np.array(node_ids, dtype=np.int64))
    N = len(node_ids_arr)

    p_dense = np.searchsorted(node_ids_arr, parents)
    c_dense = np.searchsorted(node_ids_arr, children)
    del parents, children

    order = np.argsort(p_dense, kind='stable')
    p_dense = p_dense[order]
    c_dense = c_dense[order]
    del order
    print(f"[Layer {piece_count}] dense remap in "
          f"{format_duration(time.perf_counter()-t)}", flush=True)

    # ----------------------------------------------------------
    # 4. CSR indptr
    # ----------------------------------------------------------
    indptr = np.zeros(N + 1, dtype=np.int64)
    counts = np.bincount(p_dense, minlength=N)
    indptr[1:] = np.cumsum(counts)
    del p_dense, counts

    # ----------------------------------------------------------
    # 5. CSR + SciPy SCC
    # ----------------------------------------------------------
    t = time.perf_counter()
    data = np.ones(len(c_dense), dtype=np.uint8)
    csr = csr_matrix((data, c_dense, indptr), shape=(N, N))
    del data
    print(f"[Layer {piece_count}] CSR built in "
          f"{format_duration(time.perf_counter()-t)}", flush=True)

    t = time.perf_counter()
    n_comp, labels = connected_components(csr, directed=True, connection='strong')
    print(f"[Layer {piece_count}] SciPy SCC: {n_comp:,} components "
          f"({format_duration(time.perf_counter()-t)})", flush=True)

    # ----------------------------------------------------------
    # 6. Group nodes by label, keep SCCs of size >= 2
    # ----------------------------------------------------------
    t = time.perf_counter()
    label_order   = np.argsort(labels, kind='stable')
    labels_sorted = labels[label_order]
    unique_labels, first_idx = np.unique(labels_sorted, return_index=True)
    boundaries = np.append(first_idx, len(labels_sorted))

    n_closed = n_white = n_black = n_both = 0
    rows_to_insert = []

    for k in range(len(unique_labels)):
        start = boundaries[k]
        end   = boundaries[k + 1]
        scc_size = end - start
        if scc_size < 2:
            continue

        dense_members = label_order[start:end]
        raw_members   = node_ids_arr[dense_members]
        scc_set       = set(dense_members.tolist())

        has_white_exit = 0
        has_black_exit = 0
        exit_nodes_raw = []
        white_exit_nodes_raw = []

        for d_idx in dense_members:
            for eidx in range(indptr[d_idx], indptr[d_idx + 1]):
                child_dense = c_dense[eidx]
                if child_dense not in scc_set:
                    raw_nid = int(node_ids_arr[d_idx])
                    exit_nodes_raw.append(raw_nid)
                    side = sides_dict.get(raw_nid)
                    if side == 'w':
                        has_white_exit = 1
                        white_exit_nodes_raw.append(raw_nid)
                    elif side == 'b':
                        has_black_exit = 1
                    break

        if has_white_exit and has_black_exit:
            cls = 'BOTH_ESCAPE';  n_both  += 1
        elif has_white_exit:
            cls = 'WHITE_ESCAPE'; n_white += 1
        elif has_black_exit:
            cls = 'BLACK_ESCAPE'; n_black += 1
        else:
            cls = 'CLOSED';       n_closed += 1

        is_closed = 1 if cls == 'CLOSED' else 0

        rows_to_insert.append((
            piece_count,
            int(scc_size),
            json.dumps([int(x) for x in raw_members]),
            json.dumps(exit_nodes_raw),
            len(white_exit_nodes_raw),
            has_white_exit,
            has_black_exit,
            is_closed,
            cls,
        ))

    print(f"[Layer {piece_count}] SCC analysis in "
          f"{format_duration(time.perf_counter()-t)}", flush=True)

    # ----------------------------------------------------------
    # 7. Write worker DB
    # ----------------------------------------------------------
    conn_out = sqlite3.connect(worker_db)
    cur_out = conn_out.cursor()
    cur_out.execute("""
        CREATE TABLE IF NOT EXISTS sccs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            piece_count       INTEGER,
            scc_size          INTEGER,
            nodes_json        TEXT,
            exit_nodes_json   TEXT,
            white_exit_count  INTEGER,
            has_white_exit    INTEGER,
            has_black_exit    INTEGER,
            is_closed         INTEGER,
            class             TEXT
        )
    """)
    cur_out.executemany("""
        INSERT INTO sccs (piece_count, scc_size, nodes_json, exit_nodes_json,
                          white_exit_count, has_white_exit, has_black_exit,
                          is_closed, class)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows_to_insert)
    conn_out.commit()
    conn_out.close()
    conn.close()

    total_time = time.perf_counter() - t_start
    print(f"[Layer {piece_count}] DONE in {format_duration(total_time)}",
          flush=True)

    return {
        "piece_count": piece_count,
        "nodes": len(node_ids),
        "edges": n_edges,
        "total_sccs": int(n_comp),
        "sccs_with_cycles": n_closed + n_white + n_black + n_both,
        "closed":  n_closed,
        "white":   n_white,
        "black":   n_black,
        "both":    n_both,
        "total_time": total_time,
        "worker_db": worker_db,
    }

# ------------------------------------------------------------
# Mark CLOSED SCC nodes in reachable_graph.db
# ------------------------------------------------------------
def mark_closed_sccs(closed_rows):
    if not closed_rows:
        return 0
    conn = sqlite3.connect(DB_GRAPH)
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA temp_store = MEMORY")

    # Ensure column exists (idempotent)
    try:
        cur.execute("ALTER TABLE nodes ADD COLUMN draw_type_cycle TEXT")
    except sqlite3.OperationalError:
        pass

    total_marked = 0
    BATCH = 5000
    for nodes_json in closed_rows:
        nodes_list = json.loads(nodes_json)
        for i in range(0, len(nodes_list), BATCH):
            chunk = nodes_list[i:i+BATCH]
            cur.executemany("""
                UPDATE nodes
                SET is_terminal = 1,
                    value = 'DRAW',
                    draw_type = 'cycle_closed'
                WHERE node_id = ?
            """, [(n,) for n in chunk])
            total_marked += len(chunk)
        conn.commit()
    conn.close()
    return total_marked

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    t_global = time.perf_counter()
    T = Timer()

    print("=" * 80)
    print("TARJAN SCC CYCLE DETECTION — NBBK/kbbn variant")
    print("=" * 80)
    print(f"Start time (local) : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Workers            : {NUM_WORKERS}")
    print(f"Serial layers      : {sorted(SERIAL_LAYERS)}")
    print("=" * 80)

    # ---------------- setup ----------------
    T.start('setup')
    if not os.path.exists(DB_GRAPH):
        print(f"ERROR: {DB_GRAPH} not found"); raise SystemExit(1)

    print("\nLoading nodes grouped by piece count ...")
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
    T.stop('setup')

    # ---------------- analyse ----------------
    print("\n=== PHASE: analyse (parallel) ===")
    T.start('analyse')

    all_tasks = sorted(nodes_by_pc.items(), key=lambda kv: -len(kv[1]))
    serial_tasks   = [(pc, nids) for pc, nids in all_tasks if pc in SERIAL_LAYERS]
    parallel_tasks = [(pc, nids) for pc, nids in all_tasks if pc not in SERIAL_LAYERS]

    summaries = []

    if parallel_tasks:
        print(f"Running {len(parallel_tasks)} small layers in parallel ...")
        with mp.Pool(processes=NUM_WORKERS) as pool:
            for summary in pool.imap_unordered(process_layer, parallel_tasks):
                summaries.append(summary)
                print(f"  -> Layer {summary['piece_count']}: "
                      f"{summary['sccs_with_cycles']:,} SCCs with cycles "
                      f"({format_duration(summary['total_time'])})", flush=True)

    for task in serial_tasks:
        print(f"\nRunning large layer {task[0]} serially ...")
        summary = process_layer(task)
        summaries.append(summary)
        print(f"  -> Layer {summary['piece_count']}: "
              f"{summary['sccs_with_cycles']:,} SCCs with cycles "
              f"({format_duration(summary['total_time'])})", flush=True)

    T.stop('analyse')

    # ---------------- merge ----------------
    print("\n=== PHASE: merge ===")
    T.start('merge')
    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)

    conn_out = sqlite3.connect(OUT_DB)
    cur_out = conn_out.cursor()
    cur_out.execute("""
        CREATE TABLE sccs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            piece_count       INTEGER,
            scc_size          INTEGER,
            nodes_json        TEXT,
            exit_nodes_json   TEXT,
            white_exit_count  INTEGER,
            has_white_exit    INTEGER,
            has_black_exit    INTEGER,
            is_closed         INTEGER,
            class             TEXT
        )
    """)
    cur_out.execute("""
        CREATE TABLE layer_summary (
            piece_count         INTEGER PRIMARY KEY,
            total_sccs          INTEGER,
            sccs_with_cycles    INTEGER,
            sccs_closed         INTEGER,
            sccs_white_escape   INTEGER,
            sccs_black_escape   INTEGER,
            sccs_both_escape    INTEGER,
            total_time          REAL
        )
    """)
    conn_out.commit()

    closed_rows = []
    total_cycles = total_closed = total_white = total_black = total_both = 0

    for s in summaries:
        wdb = s["worker_db"]
        if not os.path.exists(wdb):
            continue
        conn_w = sqlite3.connect(wdb)
        cur_w  = conn_w.cursor()
        cur_w.execute("""
            SELECT piece_count, scc_size, nodes_json, exit_nodes_json,
                   white_exit_count, has_white_exit, has_black_exit,
                   is_closed, class
            FROM sccs
        """)
        batch = cur_w.fetchall()
        cur_out.executemany("""
            INSERT INTO sccs (piece_count, scc_size, nodes_json, exit_nodes_json,
                              white_exit_count, has_white_exit, has_black_exit,
                              is_closed, class)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        # Remember CLOSED nodes for marking
        for row in batch:
            if row[7] == 1:  # is_closed
                closed_rows.append(row[2])   # nodes_json
        conn_w.close()

        cur_out.execute("""
            INSERT INTO layer_summary
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["piece_count"], s["total_sccs"], s["sccs_with_cycles"],
              s["closed"], s["white"], s["black"], s["both"], s["total_time"]))
        conn_out.commit()

        total_cycles += s["sccs_with_cycles"]
        total_closed += s["closed"]
        total_white  += s["white"]
        total_black  += s["black"]
        total_both   += s["both"]

    conn_out.commit()
    conn_out.close()
    T.stop('merge')

    # ---------------- mark closed SCCs ----------------
    print("\n=== PHASE: mark closed SCCs ===")
    T.start('mark')
    n_marked = mark_closed_sccs(closed_rows)
    print(f"  marked {n_marked:,} nodes as 'cycle_closed' draws")
    T.stop('mark')

    # ---------------- cleanup ----------------
    T.start('cleanup')
    removed = 0
    for s in summaries:
        wdb = s["worker_db"]
        if os.path.exists(wdb):
            try:
                os.remove(wdb); removed += 1
            except PermissionError:
                pass
    print(f"\n=== PHASE: cleanup ===")
    print(f"  removed {removed} temp DBs")
    T.stop('cleanup')

    # ---------------- summary ----------------
    total_elapsed = time.perf_counter() - t_global

    print("\n" + "=" * 80)
    print("CYCLE ANALYSIS COMPLETE")
    print("=" * 80)
    print(f"End time (local)     : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total SCCs with cycles: {total_cycles:,}")
    print(f"  CLOSED (trap)       : {total_closed:,}   (-> marked DRAW)")
    print(f"  BOTH_ESCAPE         : {total_both:,}")
    print(f"  WHITE_ESCAPE only   : {total_white:,}")
    print(f"  BLACK_ESCAPE only   : {total_black:,}")
    print(f"\nNodes marked 'cycle_closed': {n_marked:,}")
    print(f"Results saved to: {OUT_DB}")
    T.report(total_elapsed)

if __name__ == "__main__":
    main()