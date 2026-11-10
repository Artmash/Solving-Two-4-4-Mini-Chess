#!/usr/bin/env python3
# build_draw_strategy_tree.py
#
# Computes the AND-OR DRAW STRATEGY TREE for the new variant
# (NBBK;....;....;kbbn — 1 king + 2 bishops + 1 knight per side).
# Saves to draw_strategy_tree.db. The renderer reads that DB.
#
# Answers: "Does Black have a forced-draw strategy from the start?"
#          If dist[start] >= 0 → YES  → White does NOT have a winning strategy
#          If dist[start] < 0  → NO   → White has a winning strategy
#
# Usage:
#   python3 build_draw_strategy_tree.py
#   python3 build_draw_strategy_tree.py --max-nodes 8000 --max-depth 12 \
#       --max-and-children 3 --full-and-depth 2
# ----------------------------------------------------------------------

import sqlite3
import argparse
import os
import time
import sys
from collections import deque
import numpy as np

sys.setrecursionlimit(300000)

DB_GRAPH  = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"
OUT_DB    = "draw_strategy_tree.db"

# Draw types produced by the new variant's retrograde pass.
# Adjust if your classify_draws script uses different labels.
DRAW_TYPES = ('stalemate', 'insufficient_material', 'cycle')

def fmt_time(s):
    if s >= 3600:
        h = int(s // 3600); m = int((s % 3600) // 60); s -= h * 3600 + m * 60
        return f"{h}h {m:02d}m {s:05.2f}s"
    if s >= 60:
        m = int(s // 60); s -= m * 60
        return f"{m}m {s:05.2f}s"
    return f"{s:.2f}s"

# =====================================================================
def load_data():
    """
    Loads node metadata and edges as numpy arrays.
    Board strings are NOT loaded here — they are lazily fetched later
    for only the ~8000 nodes that make it into the tree.
    """
    print("Loading node metadata ...")
    t0 = time.perf_counter()
    conn = sqlite3.connect(DB_GRAPH)
    conn.execute("PRAGMA cache_size = -262144")
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")

    cur.execute("SELECT MAX(node_id) FROM nodes")
    N = cur.fetchone()[0] + 1
    print(f"  max node id: {N-1:,}")

    side  = np.full(N, -1, dtype=np.int8)
    value = np.full(N, -1, dtype=np.int8)
    tc    = np.zeros(N, dtype=np.int32)
    term  = np.zeros(N, dtype=np.int8)
    pc    = np.zeros(N, dtype=np.int8)
    bid   = np.zeros(N, dtype=np.int32)

    cur.execute("""
        SELECT n.node_id, n.side, n.value, n.total_children,
               n.is_terminal, n.draw_type, b.piece_count, n.board_id
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
    """)
    for nid, s, v, c, is_term, dt, pcnt, board_id in cur:
        side[nid]  = 0 if s == 'w' else 1
        if   v == 'WIN':  value[nid] = 1
        elif v == 'LOSS': value[nid] = 2
        elif v == 'DRAW': value[nid] = 3
        tc[nid] = c
        pc[nid] = pcnt
        bid[nid] = board_id
        if is_term and dt in DRAW_TYPES:
            term[nid] = 1

    cur.execute("SELECT value FROM state WHERE key='start_node_id'")
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("state table has no 'start_node_id' — "
                           "did the reachable-graph build complete?")
    start = int(row[0])

    print("Loading edges ...")
    t = time.perf_counter()
    cur.execute("SELECT COUNT(*) FROM edges")
    E = cur.fetchone()[0]
    parent = np.empty(E, dtype=np.int32)
    child  = np.empty(E, dtype=np.int32)
    i = 0
    cur.execute("SELECT parent_node_id, child_node_id FROM edges")
    for p, c in cur:
        parent[i] = p; child[i] = c; i += 1
    print(f"  {E:,} edges in {fmt_time(time.perf_counter()-t)}")

    conn.close()
    print(f"load_data done in {fmt_time(time.perf_counter()-t0)}")
    return dict(N=N, E=E, start=start,
                side=side, value=value, tc=tc, term=term, pc=pc, bid=bid,
                parent=parent, child=child)

# =====================================================================
def compute_dist(data):
    """
    BFS backwards from terminal draws.

    dist[v] >= 0  ⟺  v is inside the draw-forcing region:
                       Black can force reaching a terminal draw
                       (or a repetition anchor) from v.
    dist[v] < 0   ⟺  v is outside — Black cannot force a draw from v.
    """
    N = data['N']; side = data['side']; tc = data['tc']
    term = data['term']; parent = data['parent']
    child = data['child']; value = data['value']

    print("\nBuilding reverse adjacency ...")
    t = time.perf_counter()
    order = np.argsort(child, kind='stable')
    child_s = child[order]; parent_s = parent[order]
    indptr = np.zeros(N + 1, dtype=np.int64)
    counts = np.bincount(child_s, minlength=N)
    indptr[1:] = np.cumsum(counts)
    del order, counts
    print(f"  reverse in {fmt_time(time.perf_counter()-t)}")

    def bfs(anchors_mask, label):
        dist = np.full(N, -1, dtype=np.int32)
        cnt  = np.zeros(N, dtype=np.int32)
        q = deque()
        for a in np.where(anchors_mask)[0]:
            dist[a] = 0; q.append(a)
        n = len(q); t0 = time.perf_counter(); last = t0
        while q:
            cur = q.popleft(); d = int(dist[cur])
            a = int(indptr[cur]); b = int(indptr[cur+1])
            for i in range(a, b):
                p = int(parent_s[i])
                if dist[p] >= 0: continue
                if side[p] == 1:                    # Black — OR
                    dist[p] = d + 1; q.append(p); n += 1
                else:                               # White — AND
                    cnt[p] += 1
                    if tc[p] > 0 and cnt[p] == tc[p]:
                        dist[p] = d + 1; q.append(p); n += 1
            now = time.perf_counter()
            if now - last > 30:
                print(f"    {label}: {n:,} ({fmt_time(now - t0)})", flush=True)
                last = now
        print(f"  {label}: {n:,} nodes in {fmt_time(time.perf_counter()-t0)}")
        return dist

    # Pass 1: terminal draws only
    print("\nPass 1 — terminal draws")
    dist1 = bfs(term == 1, "pass1")

    # Nodes with DRAW value that aren't reachable in pass 1
    # are treated as repetition anchors (cycle membership)
    rep_anchor = (value == 3) & (dist1 < 0)
    print(f"\nRepetition anchors: {int(rep_anchor.sum()):,}")

    # Pass 2: all anchors
    print("\nPass 2 — all anchors")
    dist2 = bfs((term == 1) | rep_anchor, "pass2")

    data['dist']       = dist2
    data['rep_anchor'] = rep_anchor
    data['term_draw']  = term.astype(bool)

    # Print the headline
    start = data['start']
    d = int(dist2[start])
    print()
    print("=" * 72)
    if d >= 0:
        print(f"  START node {start}: dist = {d}")
        print(f"  → Black has a forced-draw strategy from the start.")
        print(f"  → White does NOT have a winning strategy.")
    else:
        print(f"  START node {start}: dist = {d} (outside draw region)")
        print(f"  → Black has NO draw strategy from the start.")
        print(f"  → White MUST have a winning strategy.")
    print("=" * 72)
    return data

# =====================================================================
NODE_BOARD  = 40
NODE_META_H = 18
NODE_W      = NODE_BOARD
NODE_H      = NODE_BOARD + NODE_META_H
H_GAP       = 14
V_GAP       = 55

def lazy_board(cur_boards, board_id, cache):
    """Fetch board string on demand."""
    if board_id in cache:
        return cache[board_id]
    cur_boards.execute("SELECT board FROM boards WHERE id = ?", (int(board_id),))
    row = cur_boards.fetchone()
    s = row[0] if row else ''
    cache[board_id] = s
    return s

def extract_tree(data, max_nodes, max_depth, max_and_children, full_and_depth):
    print("\nExtracting AND-OR tree (BFS order) ...")
    t = time.perf_counter()

    side  = data['side']; value = data['value']
    term  = data['term_draw']; dist = data['dist']
    bid   = data['bid']; pc = data['pc']
    start = data['start']

    conn = sqlite3.connect(DB_GRAPH)
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")
    cur_boards = cur
    board_cache = {}

    child_cache = {}
    def get_children(nid):
        if nid in child_cache: return child_cache[nid]
        cur.execute("SELECT child_node_id, move FROM edges "
                    "WHERE parent_node_id = ?", (nid,))
        r = cur.fetchall(); child_cache[nid] = r; return r

    counter = [0]; truncated = [False]

    def vname(v): return {1:'WIN', 2:'LOSS', 3:'DRAW'}.get(int(v), '?')
    def side_chr(s): return 'w' if s == 0 else 'b'

    def make_node(nid, kind, move, branch_side, depth):
        nid = int(nid)
        return {
            'tree_id': counter[0], 'kind': kind,
            'side': side_chr(side[nid]), 'value': vname(value[nid]),
            'pc': int(pc[nid]),
            'board': lazy_board(cur_boards, bid[nid], board_cache),
            'reachable_nid': nid, 'move': move, 'branch_side': branch_side,
            'depth': depth, 'children': [], 'rep_step': None,
        }

    root = make_node(start, 'TBD', None, None, 0)
    counter[0] += 1

    queue = deque()
    queue.append((root, int(start), (), 0))

    while queue:
        node, nid, ancestors, depth = queue.popleft()

        if term[nid]:
            node['kind'] = 'TERM'
            continue

        children = get_children(nid)
        if not children:
            node['kind'] = 'DEAD'
            continue

        new_anc = ancestors + (nid,)

        # ---------- OR node: Black to move ----------
        # Black chooses ONE move that stays inside the draw region.
        if node['side'] == 'b':
            node['kind'] = 'OR'
            chosen = None

            # Prefer a repetition move if available
            for cid, mv in children:
                if cid in new_anc:
                    chosen = (cid, mv); break

            # Otherwise, prefer a child that reduces distance by exactly 1
            if chosen is None and int(dist[nid]) > 0:
                target = int(dist[nid]) - 1
                for cid, mv in children:
                    if int(dist[cid]) == target:
                        chosen = (cid, mv); break

            # Fallback: any child inside the draw region
            if chosen is None:
                cands = [(cid, mv) for cid, mv in children if dist[cid] >= 0]
                if cands:
                    cands.sort(key=lambda x: int(dist[x[0]]))
                    chosen = cands[0]

            if chosen is None:
                node['kind'] = 'DEAD'
                continue

            cid, mv = chosen

            if cid in new_anc:
                child = make_node(cid, 'REP', mv, 'b', depth + 1)
                node['children'].append(child); counter[0] += 1
                continue

            if depth + 1 > max_depth or counter[0] >= max_nodes:
                truncated[0] = True
                child = make_node(cid, 'CUT', mv, 'b', depth + 1)
                node['children'].append(child); counter[0] += 1
                continue

            child = make_node(cid, 'TBD', mv, 'b', depth + 1)
            node['children'].append(child); counter[0] += 1
            queue.append((child, cid, new_anc, depth + 1))

        # ---------- AND node: White to move ----------
        # White chooses — we must show ALL White moves.
        # (Inside the draw region, every child has dist >= 0, so the
        #  "skip dist < 0" line is a safety belt, not the main filter.)
        else:
            node['kind'] = 'AND'
            cap = 10**9 if depth <= full_and_depth else max_and_children

            count = 0
            for cid, mv in children:
                if int(dist[cid]) < 0:
                    continue
                if count >= cap:
                    truncated[0] = True
                    break
                if cid in new_anc:
                    child = make_node(cid, 'REP', mv, 'w', depth + 1)
                    node['children'].append(child); counter[0] += 1
                    count += 1
                    continue
                if depth + 1 > max_depth:
                    truncated[0] = True
                    child = make_node(cid, 'CUT', mv, 'w', depth + 1)
                    node['children'].append(child); counter[0] += 1
                    count += 1
                    continue
                if counter[0] >= max_nodes:
                    truncated[0] = True
                    break
                child = make_node(cid, 'TBD', mv, 'w', depth + 1)
                node['children'].append(child); counter[0] += 1
                queue.append((child, cid, new_anc, depth + 1))
                count += 1

            if not node['children']:
                node['kind'] = 'DEAD'

    conn.close()

    # ---------- Layout ----------
    print("Computing layout ...")
    t2 = time.perf_counter()
    cursor = [0]
    def layout(node, depth):
        node['depth'] = depth
        if not node['children']:
            node['x'] = cursor[0]
            cursor[0] += NODE_W + H_GAP
        else:
            for c in node['children']:
                layout(c, depth + 1)
            xs = [c['x'] for c in node['children']]
            node['x'] = (min(xs) + max(xs)) / 2.0
        node['y'] = depth * (NODE_H + V_GAP)
    layout(root, 0)
    print(f"  layout done in {fmt_time(time.perf_counter()-t2)}, "
          f"canvas ≈ {cursor[0]:.0f} × "
          f"{(max_depth+1) * (NODE_H + V_GAP):.0f} px")

    print(f"  extracted {counter[0]:,} nodes in {fmt_time(time.perf_counter()-t)}")
    print(f"  truncated: {truncated[0]}")
    return root, counter[0], truncated[0]

# =====================================================================
def save_to_db(root, data, n_nodes, truncated, db_path, args):
    print(f"\nSaving to {db_path} ...")
    t = time.perf_counter()
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("""
        CREATE TABLE tree_nodes (
            tree_id       INTEGER PRIMARY KEY,
            parent_id     INTEGER,
            kind          TEXT    NOT NULL,
            side          TEXT,
            value         TEXT,
            piece_count   INTEGER,
            board         TEXT,
            move          TEXT,
            branch_side   TEXT,
            depth         INTEGER,
            x             REAL,
            y             REAL,
            rep_step      INTEGER,
            reachable_nid INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE tree_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()

    rows = []
    def walk(node, parent_id):
        rows.append((
            node['tree_id'], parent_id, node['kind'], node['side'],
            node['value'], node['pc'], node['board'],
            node['move'], node['branch_side'], node['depth'],
            float(node['x']), float(node['y']),
            node.get('rep_step'), node['reachable_nid'],
        ))
        for c in node['children']:
            walk(c, node['tree_id'])
    walk(root, None)
    cur.executemany("""
        INSERT INTO tree_nodes
        (tree_id, parent_id, kind, side, value, piece_count, board,
         move, branch_side, depth, x, y, rep_step, reachable_nid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)

    root_dist = int(data['dist'][data['start']])
    max_pc = max((r[5] for r in rows if r[5] is not None), default=0)
    meta = {
        'start_nid':              str(data['start']),
        'start_tree_id':          str(root['tree_id']),
        'root_draw_distance':     str(root_dist),
        'black_has_draw':         'yes' if root_dist >= 0 else 'no',
        'white_has_win':          'no'  if root_dist >= 0 else 'yes',
        'total_terminal_draws':   str(int(data['term_draw'].sum())),
        'total_repetition_anchors': str(int(data['rep_anchor'].sum())),
        'nodes_in_tree':          str(n_nodes),
        'max_piece_count_in_tree': str(max_pc),
        'truncated':              'yes' if truncated else 'no',
        'max_nodes_param':        str(args.max_nodes),
        'max_depth_param':        str(args.max_depth),
        'max_and_children_param': str(args.max_and_children),
        'full_and_depth_param':   str(args.full_and_depth),
        'built_at':               time.strftime('%Y-%m-%d %H:%M:%S'),
        'canvas_width':           f"{max(r[10] for r in rows) + NODE_W:.0f}",
        'canvas_height':          f"{max(r[11] for r in rows) + NODE_H:.0f}",
    }
    cur.executemany("REPLACE INTO tree_meta (key, value) VALUES (?, ?)",
                    list(meta.items()))
    conn.commit()
    conn.close()
    print(f"  saved {len(rows):,} nodes in {fmt_time(time.perf_counter()-t)}")
    return meta

# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out',              type=str, default=OUT_DB)
    ap.add_argument('--max-nodes',        type=int, default=8000)
    ap.add_argument('--max-depth',        type=int, default=12)
    ap.add_argument('--max-and-children', type=int, default=3)
    ap.add_argument('--full-and-depth',   type=int, default=2,
                    help='expand ALL White moves at AND nodes up to this depth')
    args = ap.parse_args()

    t0 = time.perf_counter()
    print("=" * 72)
    print("BUILD DRAW STRATEGY TREE — NBBK/kbbn new variant")
    print("=" * 72)
    print(f"  max-nodes        : {args.max_nodes}")
    print(f"  max-depth        : {args.max_depth}")
    print(f"  max-and-children : {args.max_and_children}")
    print(f"  full-and-depth   : {args.full_and_depth}")
    print("=" * 72)

    data = load_data()
    data = compute_dist(data)
    root, n_nodes, truncated = extract_tree(
        data, args.max_nodes, args.max_depth,
        args.max_and_children, args.full_and_depth)
    meta = save_to_db(root, data, n_nodes, truncated, args.out, args)

    print("\n" + "=" * 72)
    print("DONE")
    print("=" * 72)
    for k, v in meta.items():
        print(f"  {k:<28s}: {v}")
    print(f"\nTotal: {fmt_time(time.perf_counter()-t0)}")
    print(f"\nNext: python3 render_draw_strategy.py")

if __name__ == '__main__':
    main()