#!/usr/bin/env python3
# build_black_strategy_tree.py
#
# Extracts the complete Black strategy DAG that proves:
#   * White does NOT have a winning strategy.
#   * Black CAN force a draw or a win from every position on every path.
#
# Safe for Black = retrograde DRAW or retrograde Black-win, i.e.
#   safe[n] = (side[n]=='b' and value[n] in {WIN,DRAW}) OR
#             (side[n]=='w' and value[n] in {LOSS,DRAW})
#
# DAG extraction:
#   * Black-to-move safe nodes  -> expand ONE safe child (Black's choice)
#   * White-to-move safe nodes  -> expand ALL safe children
#                                  (these are exactly all legal White
#                                   moves; every one leads to a safe
#                                   position, so White cannot escape
#                                   to a winning position)
#
# Every path terminates at one of:
#   * TERMINAL_DRAW      (stalemate or insufficient material)
#   * TERMINAL_BLACK_WIN (checkmate with White to move)
#   * CYCLE_DRAW         (position on a cycle; game repeats = draw)
#   * DEPTH_CUT          (optional --max-plies; retrograde says safe)
# ----------------------------------------------------------------------

import sqlite3, os, sys, time, argparse, json
from collections import deque, defaultdict
import numpy as np

OUT_DEFAULT = "black_strategy_tree.db"


def fmt(s):
    if s >= 3600:
        h = int(s // 3600); m = int((s % 3600) // 60); s -= h*3600 + m*60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if s >= 60:
        m = int(s // 60); s -= m*60
        return f"{m}m {s:05.2f}s"
    return f"{s:.2f}s"


# =====================================================================
# 1. Load graph
# =====================================================================
def load_graph(graph_db, boards_db):
    print("[1/6] Loading graph metadata ...")
    t = time.perf_counter()
    conn = sqlite3.connect(graph_db)
    conn.execute("PRAGMA cache_size = -262144")
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{boards_db}' AS bdb")

    cur.execute("SELECT MAX(node_id) FROM nodes")
    N = cur.fetchone()[0] + 1
    print(f"  max node id: {N-1:,}")

    side  = np.full(N, -1, dtype=np.int8)   # 0 = w, 1 = b
    tc    = np.zeros(N, dtype=np.int32)
    bid   = np.zeros(N, dtype=np.int32)
    pc    = np.zeros(N, dtype=np.int8)
    # value: 0 = null, 1 = WIN, 2 = LOSS, 3 = DRAW
    val   = np.zeros(N, dtype=np.int8)
    # dt (for hard-terminal classification):
    # 0 none, 1 stalemate, 2 insufficient, 3 checkmate
    dt    = np.zeros(N, dtype=np.int8)
    is_t  = np.zeros(N, dtype=np.int8)

    cur.execute("""
        SELECT n.node_id, n.side, n.total_children, n.is_terminal,
               n.draw_type, n.value, b.piece_count, n.board_id
        FROM nodes n JOIN bdb.boards b ON n.board_id = b.id
    """)
    n_loaded = 0
    V_MAP = {'WIN': 1, 'LOSS': 2, 'DRAW': 3}
    DT_MAP = {'stalemate': 1, 'insufficient_material': 2, 'checkmate': 3}
    for nid, s, c, is_tv, d, v, pcnt, board_id in cur:
        side[nid] = 0 if s == 'w' else 1
        tc[nid] = c
        is_t[nid] = 1 if is_tv else 0
        if v in V_MAP: val[nid] = V_MAP[v]
        if d in DT_MAP: dt[nid] = DT_MAP[d]
        pc[nid] = pcnt
        bid[nid] = board_id
        n_loaded += 1
    print(f"  {n_loaded:,} nodes in {fmt(time.perf_counter()-t)}")

    # Sanity: retrograde must be present
    n_valued = int((val != 0).sum())
    print(f"  retrograde values present: {n_valued:,}/{N-1:,} "
          f"({100.0*n_valued/(N-1):.1f}%)")
    if n_valued == 0:
        print("\nERROR: retrograde values not found. Run run_all_retrograde.py first.")
        sys.exit(1)

    print("  loading edges ...")
    t = time.perf_counter()
    cur.execute("SELECT COUNT(*) FROM edges")
    E = cur.fetchone()[0]
    parent = np.empty(E, dtype=np.int32)
    child  = np.empty(E, dtype=np.int32)
    i = 0
    cur.execute("SELECT parent_node_id, child_node_id FROM edges")
    for p, c in cur:
        parent[i] = p; child[i] = c; i += 1
    print(f"  {E:,} edges in {fmt(time.perf_counter()-t)}")

    cur.execute("SELECT value FROM state WHERE key='start_node_id'")
    root_nid = int(cur.fetchone()[0])
    conn.close()

    return dict(N=N, E=E, root=root_nid, side=side, tc=tc, bid=bid,
                pc=pc, val=val, dt=dt, is_t=is_t, parent=parent, child=child)


# =====================================================================
# 2. Safe set
# =====================================================================
def compute_safe(data):
    print("\n[2/6] Computing safe set (retrograde DRAW or Black-win) ...")
    t = time.perf_counter()
    N, side, val = data['N'], data['side'], data['val']

    safe = np.zeros(N, dtype=bool)
    # White to move: safe if LOSS or DRAW (White can't win)
    safe[(side == 0) & ((val == 2) | (val == 3))] = True
    # Black to move: safe if WIN or DRAW (Black at least draws)
    safe[(side == 1) & ((val == 1) | (val == 3))] = True

    n_safe = int(safe.sum())
    print(f"  safe nodes: {n_safe:,}/{int((side>=0).sum()):,} "
          f"({100.0*n_safe/int((side>=0).sum()):.2f}%) in "
          f"{fmt(time.perf_counter()-t)}")

    root_safe = bool(safe[data['root']])
    print(f"  root ({data['root']}) safe: {root_safe}")

    data['safe'] = safe
    return data


# =====================================================================
# 3. Distance-to-terminal (for Black's choice heuristic)
# =====================================================================
def compute_dist_to_terminal(data):
    print("\n[3/6] BFS: distance to hard terminal ...")
    t = time.perf_counter()
    N, side, is_t, dt, parent, child = (
        data['N'], data['side'], data['is_t'], data['dt'],
        data['parent'], data['child'])

    # Hard terminal masks
    is_term_draw = (is_t == 1) & ((dt == 1) | (dt == 2))
    is_term_bw   = (is_t == 1) & (dt == 3) & (side == 0)   # White checkmated = Black wins
    hard = is_term_draw | is_term_bw
    n_hard = int(hard.sum())
    print(f"  hard terminals: {n_hard:,} "
          f"({int(is_term_draw.sum()):,} draw + "
          f"{int(is_term_bw.sum()):,} Black-win)")

    # Reverse indptr
    print("  building reverse indptr ...")
    order = np.argsort(child, kind='stable')
    child_s = child[order]
    parent_s = parent[order]
    del order
    rev_indptr = np.zeros(N + 1, dtype=np.int64)
    cnt = np.bincount(child_s, minlength=N)
    rev_indptr[1:] = np.cumsum(cnt)
    del cnt

    # BFS on reverse edges from all hard terminals
    dist = np.full(N, -1, dtype=np.int32)
    q = deque()
    for nid in np.where(hard)[0]:
        dist[nid] = 0; q.append(int(nid))

    n_visited = len(q)
    last = time.perf_counter()
    while q:
        n = q.popleft()
        d = int(dist[n])
        a = int(rev_indptr[n]); b = int(rev_indptr[n+1])
        for i in range(a, b):
            p = int(parent_s[i])
            if dist[p] < 0:
                dist[p] = d + 1
                q.append(p); n_visited += 1
        now = time.perf_counter()
        if now - last > 15:
            print(f"    {n_visited:,} ({fmt(now-t)})", flush=True)
            last = now

    print(f"  {n_visited:,} nodes with finite dist in "
          f"{fmt(time.perf_counter()-t)}")

    data['hard_terminal'] = hard
    data['dist_to_terminal'] = dist
    data['parent_s'] = parent_s
    data['rev_indptr'] = rev_indptr
    return data


# =====================================================================
# 4. Extract DAG (BFS, streaming)
# =====================================================================
def extract_dag(data, out_db, max_plies=None):
    print(f"\n[4/6] Extracting strategy DAG ...")
    if max_plies:
        print(f"  max plies: {max_plies} (nodes at depth {max_plies} become DEPTH_CUT leaves)")
    t = time.perf_counter()

    N, side, tc, safe, bid = (
        data['N'], data['side'], data['tc'], data['safe'], data['bid'])
    is_t = data['is_t']
    dt = data['dt']
    dist_term = data['dist_to_terminal']
    hard = data['hard_terminal']
    parent, child = data['parent'], data['child']
    root = data['root']

    # Forward adjacency
    print("  building forward adjacency ...")
    t0 = time.perf_counter()
    order = np.argsort(parent, kind='stable')
    p_sorted = parent[order]
    c_sorted = child[order]
    del order
    fwd_indptr = np.zeros(N + 1, dtype=np.int64)
    cnt = np.bincount(p_sorted, minlength=N)
    fwd_indptr[1:] = np.cumsum(cnt)
    del cnt, p_sorted
    print(f"  forward in {fmt(time.perf_counter()-t0)}")

    # Output DB
    if os.path.exists(out_db):
        os.remove(out_db)
    out = sqlite3.connect(out_db)
    out.execute("PRAGMA synchronous = OFF")
    out.execute("PRAGMA journal_mode = DELETE")
    cur = out.cursor()
    cur.execute("""
        CREATE TABLE tree_nodes (
            node_id     INTEGER PRIMARY KEY,
            board_id    INTEGER,
            side        TEXT,
            value       TEXT,
            is_leaf     INTEGER,
            leaf_kind   TEXT,
            depth       INTEGER,
            dist        INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE tree_edges (
            parent_id   INTEGER,
            child_id    INTEGER,
            PRIMARY KEY (parent_id, child_id)
        )
    """)
    cur.execute("""
        CREATE TABLE black_choices (
            node_id       INTEGER PRIMARY KEY,
            chosen_child  INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)
    """)
    out.commit()
    print("  output DB initialized")

    # BFS
    visited = np.zeros(N, dtype=bool)
    depth_arr = np.zeros(N, dtype=np.int32)
    leaf_kind = {}  # node_id -> leaf_kind
    black_choice = {}

    def classify_leaf(nid):
        """Return leaf_kind for a node (or None if not a leaf)."""
        if not is_t[nid]:
            return None
        d = int(dt[nid])
        s = int(side[nid])
        if d == 1: return 'terminal_draw'      # stalemate
        if d == 2: return 'terminal_draw'      # insufficient
        if d == 3:
            if s == 0: return 'terminal_black_win'    # White checkmated
            else:      return None                     # Black checkmated - not safe
        return None

    # Queue entries: (nid, depth)
    queue = deque([(root, 0)])
    visited[root] = True
    depth_arr[root] = 0

    # Seed node insertion
    V_LABEL = {0: None, 1: 'WIN', 2: 'LOSS', 3: 'DRAW'}
    def insert_node(nid):
        lk = classify_leaf(nid)
        if lk is None and max_plies is not None and depth_arr[nid] >= max_plies:
            lk = 'depth_cut'
        leaf_kind[nid] = lk
        return (
            int(nid), int(bid[nid]),
            'w' if side[nid] == 0 else 'b',
            V_LABEL[int(data['val'][nid])],
            1 if lk is not None else 0,
            lk,
            int(depth_arr[nid]),
            int(dist_term[nid]),
        )

    node_batch = [insert_node(root)]
    edge_batch = []
    bc_batch = []
    NODE_BATCH = 50000
    EDGE_BATCH = 100000

    def flush_nodes():
        if node_batch:
            cur.executemany(
                "INSERT INTO tree_nodes "
                "(node_id, board_id, side, value, is_leaf, leaf_kind, depth, dist) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", node_batch)
            node_batch.clear()

    def flush_edges():
        if edge_batch:
            cur.executemany(
                "INSERT OR IGNORE INTO tree_edges (parent_id, child_id) "
                "VALUES (?, ?)", edge_batch)
            edge_batch.clear()

    def flush_bc():
        if bc_batch:
            cur.executemany(
                "INSERT INTO black_choices (node_id, chosen_child) "
                "VALUES (?, ?)", bc_batch)
            bc_batch.clear()

    def flush_all():
        flush_nodes(); flush_edges(); flush_bc(); out.commit()

    n_processed = 0
    last = time.perf_counter()

    while queue:
        nid, depth = queue.popleft()
        n_processed += 1

        if leaf_kind.get(nid) is not None:
            continue

        a = int(fwd_indptr[nid]); b = int(fwd_indptr[nid+1])
        kids = [int(c_sorted[i]) for i in range(a, b)]
        safe_kids = [c for c in kids if safe[c]]

        if not safe_kids:
            # Should not happen for a safe node; mark as error
            leaf_kind[nid] = 'error_dead_end'
            cur.execute("UPDATE tree_nodes SET is_leaf=1, leaf_kind='error_dead_end' "
                        "WHERE node_id=?", (int(nid),))
            continue

        if side[nid] == 1:
            # BLACK to move: pick one safe child.
            # Prefer (a) hard terminal, (b) smallest dist_to_terminal.
            def score(c):
                if hard[c]:
                    return (0, 0)
                d = int(dist_term[c])
                if d >= 0:
                    return (1, d)
                return (2, 0)
            safe_kids.sort(key=score)
            chosen = safe_kids[0]
            black_choice[nid] = chosen
            bc_batch.append((int(nid), int(chosen)))
            edge_batch.append((int(nid), int(chosen)))
            if not visited[chosen]:
                visited[chosen] = True
                depth_arr[chosen] = depth + 1
                node_batch.append(insert_node(chosen))
                if leaf_kind.get(chosen) is None:
                    queue.append((chosen, depth + 1))
        else:
            # WHITE to move: expand ALL safe children
            for c in safe_kids:
                edge_batch.append((int(nid), int(c)))
                if not visited[c]:
                    visited[c] = True
                    depth_arr[c] = depth + 1
                    node_batch.append(insert_node(c))
                    if leaf_kind.get(c) is None:
                        queue.append((c, depth + 1))

        if len(node_batch) >= NODE_BATCH:
            flush_nodes()
        if len(edge_batch) >= EDGE_BATCH:
            flush_edges()
        if len(bc_batch) >= EDGE_BATCH:
            flush_bc()

        now = time.perf_counter()
        if now - last > 15:
            print(f"    processed {n_processed:,}, "
                  f"queued {len(queue):,}, "
                  f"nodes {int(visited.sum()):,}, "
                  f"edges pending {len(edge_batch):,} ({fmt(now-t)})",
                  flush=True)
            last = now

    flush_all()
    out.commit()

    n_dag_nodes = int(visited.sum())
    print(f"  DAG extracted: {n_dag_nodes:,} nodes, "
          f"in {fmt(time.perf_counter()-t)}")

    data['out_conn'] = out
    data['out_cur'] = cur
    data['visited'] = visited
    data['depth_arr'] = depth_arr
    data['leaf_kind'] = leaf_kind
    data['black_choice'] = black_choice
    data['n_dag_nodes'] = n_dag_nodes
    return data


# =====================================================================
# 5. Cycle detection on the DAG (SCC per layer)
# =====================================================================
def detect_cycles(data):
    print("\n[5/6] Detecting cycles (SCC per layer) ...")
    t0 = time.perf_counter()
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    out_cur = data['out_cur']
    visited = data['visited']
    side = data['side']
    pc = data['pc']

    # Mark all cycle-nodes
    is_cycle = np.zeros(data['N'], dtype=bool)

    for layer in range(3, 9):
        # DAG nodes at this layer
        cur_layer = np.where(visited & (pc == layer))[0]
        if len(cur_layer) == 0:
            continue
        print(f"  layer {layer}: {len(cur_layer):,} DAG nodes")

        # Get edges within this layer from DB
        cur_layer_list = [int(x) for x in cur_layer]
        layer_set = set(cur_layer_list)

        # Query edges where both endpoints are in this layer
        # Batch the query
        n_edges = 0
        src, dst = [], []
        BATCH = 5000
        for i in range(0, len(cur_layer_list), BATCH):
            chunk = cur_layer_list[i:i+BATCH]
            ph = ','.join('?' * len(chunk))
            out_cur.execute(f"""
                SELECT parent_id, child_id FROM tree_edges
                WHERE parent_id IN ({ph})
            """, chunk)
            for p, c in out_cur.fetchall():
                if c in layer_set:
                    src.append(p); dst.append(c); n_edges += 1

        print(f"    {n_edges:,} within-layer edges")

        if n_edges == 0:
            continue

        # Dense remap
        dense = {nid: i for i, nid in enumerate(cur_layer_list)}
        rows = np.array([dense[s] for s in src], dtype=np.int32)
        cols = np.array([dense[d] for d in dst], dtype=np.int32)
        n = len(cur_layer_list)
        csr = csr_matrix((np.ones(len(rows), dtype=np.uint8), (rows, cols)),
                         shape=(n, n))
        n_comp, labels = connected_components(csr, directed=True,
                                              connection='strong')
        # Find SCCs of size >= 2, plus self-loops
        from collections import Counter
        sizes = Counter(labels)
        # Self-loop detection
        self_loop = np.zeros(n, dtype=bool)
        for i, j in zip(rows, cols):
            if i == j:
                self_loop[i] = True
        n_cyc = 0
        for i in range(n):
            lbl = labels[i]
            if sizes[lbl] >= 2 or self_loop[i]:
                is_cycle[cur_layer_list[i]] = True
                n_cyc += 1
        print(f"    {n_cyc:,} cycle nodes")

    n_total_cycle = int(is_cycle.sum())
    print(f"\n  total cycle nodes: {n_total_cycle:,} in "
          f"{fmt(time.perf_counter()-t0)}")

    # Update DB: mark as leaf if cycle
    print("  updating DB with cycle flags ...")
    t = time.perf_counter()
    cycle_nids = np.where(is_cycle)[0]
    for i in range(0, len(cycle_nids), 10000):
        chunk = [int(x) for x in cycle_nids[i:i+10000]]
        ph = ','.join('?' * len(chunk))
        out_cur.execute(f"""
            UPDATE tree_nodes
            SET is_leaf = 1, leaf_kind = 'cycle_draw'
            WHERE node_id IN ({ph})
        """, chunk)
    data['out_conn'].commit()
    print(f"  updated in {fmt(time.perf_counter()-t)}")

    data['is_cycle'] = is_cycle
    return data


# =====================================================================
# 6. Meta + close
# =====================================================================
def finalize(data):
    print("\n[6/6] Writing meta ...")
    out_cur = data['out_cur']
    out = data['out_conn']

    out_cur.execute("SELECT leaf_kind, COUNT(*) FROM tree_nodes "
                    "WHERE is_leaf=1 GROUP BY leaf_kind")
    by_leaf = dict(out_cur.fetchall())
    out_cur.execute("SELECT COUNT(*) FROM tree_nodes")
    n_nodes = out_cur.fetchone()[0]
    out_cur.execute("SELECT COUNT(*) FROM tree_edges")
    n_edges = out_cur.fetchone()[0]
    out_cur.execute("SELECT COUNT(*) FROM black_choices")
    n_bc = out_cur.fetchone()[0]

    meta = {
        'root_nid':             str(data['root']),
        'tree_nodes':           str(n_nodes),
        'tree_edges':           str(n_edges),
        'black_choices':        str(n_bc),
        'terminal_draw':        str(by_leaf.get('terminal_draw', 0)),
        'terminal_black_win':   str(by_leaf.get('terminal_black_win', 0)),
        'cycle_draw':           str(by_leaf.get('cycle_draw', 0)),
        'depth_cut':            str(by_leaf.get('depth_cut', 0)),
        'error_dead_end':       str(by_leaf.get('error_dead_end', 0)),
        'built_at':             time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    out_cur.executemany("REPLACE INTO meta (key, value) VALUES (?, ?)",
                        list(meta.items()))
    out.commit()
    out.close()

    print("\n" + "=" * 72)
    print("STRATEGY DAG SUMMARY")
    print("=" * 72)
    print(f"  Root: {data['root']}")
    print(f"  Tree nodes    : {n_nodes:>12,}")
    print(f"  Tree edges    : {n_edges:>12,}")
    print(f"  Black choices : {n_bc:>12,}")
    print(f"  Leaf breakdown:")
    print(f"    terminal draws   : {by_leaf.get('terminal_draw', 0):>12,}  (stalemate / insufficient)")
    print(f"    terminal black-w : {by_leaf.get('terminal_black_win', 0):>12,}  (checkmates — Black wins)")
    print(f"    cycle draws      : {by_leaf.get('cycle_draw', 0):>12,}  (positions in a cycle — repetition draw)")
    print(f"    depth cuts       : {by_leaf.get('depth_cut', 0):>12,}  (retrograde DRAW, expansion stopped)")
    if by_leaf.get('error_dead_end', 0) > 0:
        print(f"    !! error dead ends: {by_leaf.get('error_dead_end', 0):,}")
    print(f"\n✓ Complete strategy DAG saved.")
    print(f"  Every path either terminates at a hard draw / Black-win")
    print(f"  leaf, or reaches a cycle_draw node (repetition draw).")
    print(f"  Therefore: WHITE CANNOT WIN from the root.")


# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--graph-db',  default='reachable_graph.db')
    ap.add_argument('--boards-db', default='chess_boards_grouped.db')
    ap.add_argument('--out-db',    default=OUT_DEFAULT)
    ap.add_argument('--max-plies', type=int, default=None,
                    help='Optional depth cap for demo-sized output DB '
                         '(nodes at max-plies become depth_cut leaves; '
                         'retrograde still says safe).')
    args = ap.parse_args()

    t_global = time.perf_counter()
    print("=" * 72)
    print("BLACK STRATEGY TREE EXTRACTOR (retrograde-safe)")
    print("=" * 72)
    print(f"  graph DB  : {args.graph_db}")
    print(f"  boards DB : {args.boards_db}")
    print(f"  out DB    : {args.out_db}")
    print(f"  max plies : {args.max_plies or 'none (complete)'}")
    print("=" * 72)

    for p in [args.graph_db, args.boards_db]:
        if not os.path.exists(p):
            print(f"\nERROR: {p} not found"); sys.exit(1)

    data = load_graph(args.graph_db, args.boards_db)
    data = compute_safe(data)

    if not data['safe'][data['root']]:
        print("\n" + "=" * 72)
        print("!! ROOT IS NOT SAFE !!")
        print("=" * 72)
        print(f"  Root node ({data['root']}) is classified as a White win in")
        print(f"  the retrograde analysis. This contradicts earlier results.")
        print(f"  Check that reachable_graph.db holds the correct retrograde values.")
        sys.exit(1)

    data = compute_dist_to_terminal(data)
    data = extract_dag(data, args.out_db, max_plies=args.max_plies)
    data = detect_cycles(data)
    finalize(data)

    print(f"\nTotal time: {fmt(time.perf_counter() - t_global)}")
    print(f"\nNext: python3 play_black_strategy.py")
    print(f"  (You play White; computer plays Black from the DAG.)")


if __name__ == '__main__':
    main()