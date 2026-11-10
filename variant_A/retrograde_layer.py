#!/usr/bin/env python3
# retrograde_layer.py
# Retrograde analysis for ONE layer, run bottom-up (3, 4, 5, 6, 7, 8).
# Reads values of lower layers from DB, computes values for this layer.
#
# Usage: python retrograde_layer.py --layer 5

import sqlite3
import time
import argparse
from collections import deque

DB_GRAPH = "reachable_graph.db"
DB_BOARDS = "chess_boards_grouped.db"
FLUSH_BATCH = 20000

# ---------- chess helpers ----------
def find_king(b, s):
    k = 'K' if s == 'w' else 'k'
    for r in range(4):
        for c in range(4):
            if b[r][c] == k:
                return (r, c)
    return None

def is_attacked(b, r, c, by):
    for i in range(4):
        for j in range(4):
            p = b[i][j]
            if p == '.':
                continue
            iw = p.isupper()
            if (by == 'w' and not iw) or (by == 'b' and iw):
                continue
            pl = p.lower()
            if pl == 'k':
                if abs(i - r) <= 1 and abs(j - c) <= 1 and (i, j) != (r, c):
                    return True
            elif pl == 'n':
                if (abs(i - r), abs(j - c)) in [(1, 2), (2, 1)]:
                    return True
            elif pl == 'b':
                dr, dc = r - i, c - j
                if abs(dr) == abs(dc) and dr:
                    sr = 1 if dr > 0 else -1
                    sc = 1 if dc > 0 else -1
                    rr, cc = i + sr, j + sc
                    ok = True
                    while (rr, cc) != (r, c):
                        if b[rr][cc] != '.':
                            ok = False
                            break
                        rr += sr
                        cc += sc
                    if ok:
                        return True
    return False

def is_in_check(b, s):
    kp = find_king(b, s)
    if not kp:
        return False
    opp = 'b' if s == 'w' else 'w'
    return is_attacked(b, kp[0], kp[1], opp)

def str_to_board(s):
    return [list(r) for r in s.split(';')]

# ---------- main ----------
def solve_layer(L):
    t0 = time.time()
    print("=" * 72)
    print(f"RETROGRADE — LAYER {L}")
    print("=" * 72)

    conn = sqlite3.connect(DB_GRAPH, timeout=180)
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")
    conn.execute("PRAGMA temp_store=MEMORY")
    cur = conn.cursor()
    cur.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")

    # Ensure columns exist
    for col, typ in [('value', 'TEXT'), ('win_children', 'INTEGER DEFAULT 0')]:
        try:
            cur.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()

    # ---------- 1. Load layer nodes ----------
    print(f"\n[1/6] Loading layer {L}...")
    t = time.time()
    cur.execute("""
        SELECT n.node_id, n.side, n.total_children
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
        WHERE b.piece_count = ?
    """, (L,))
    rows = cur.fetchall()
    layer_nodes = {}
    total_children = {}
    side_of = {}
    for nid, side, tc in rows:
        layer_nodes[nid] = True
        total_children[nid] = tc
        side_of[nid] = side
    N = len(layer_nodes)
    print(f"  {N:,} nodes in {time.time()-t:.1f}s")
    if N == 0:
        conn.close()
        return

    # ---------- 2. Reset values in this layer ----------
    print(f"\n[2/6] Resetting values for layer {L}...")
    t = time.time()
    cur.execute("""
        UPDATE nodes SET value = NULL, win_children = 0
        WHERE node_id IN (
            SELECT n.node_id FROM nodes n
            JOIN boards_db.boards b ON n.board_id = b.id
            WHERE b.piece_count = ?
        )
    """, (L,))
    conn.commit()
    print(f"  Done in {time.time()-t:.1f}s")

    # ---------- 3. Initialise terminals ----------
    print(f"\n[3/6] Initialising terminals...")
    t = time.time()
    cur.execute("""
        SELECT n.node_id, n.side, n.draw_type, b.board
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
        WHERE b.piece_count = ? AND n.total_children = 0
    """, (L,))
    terminals = cur.fetchall()

    cur_values = {}   # nid -> 'WIN'/'LOSS'/'DRAW'
    cur_wc = {}       # nid -> current win_children count
    queue = deque()

    for nid, side, dt, bs in terminals:
        if dt == 'insufficient_material':
            v = 'DRAW'
        else:
            b = str_to_board(bs)
            v = 'LOSS' if is_in_check(b, side) else 'DRAW'
        cur_values[nid] = v
        cur_wc[nid] = 0
        if v == 'LOSS':
            queue.append(nid)

    print(f"  {len(terminals)} terminals, {len(queue)} queued "
          f"in {time.time()-t:.1f}s")

    # ---------- 4. Precompute initial win_children ----------
    print(f"\n[4/6] Precomputing win_children from lower layers...")
    t = time.time()
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
    has_loss = set()
    for p, v in cur.fetchall():
        if v == 'WIN':
            cur_wc[p] = cur_wc.get(p, 0) + 1
        elif v == 'LOSS':
            has_loss.add(p)

    for nid in layer_nodes:
        if nid in cur_values:
            continue
        wc = cur_wc.get(nid, 0)
        tc = total_children[nid]
        if nid in has_loss:
            cur_values[nid] = 'WIN'
            queue.append(nid)
        elif tc > 0 and wc == tc:
            cur_values[nid] = 'LOSS'
            queue.append(nid)

    print(f"  Precompute done in {time.time()-t:.1f}s, "
          f"queue now {len(queue):,}")

    # ---------- 5. Build reverse map for same-layer edges ----------
    print(f"\n[5/6] Building reverse map for same-layer edges...")
    t = time.time()
    cur.execute("""
        SELECT e.child_node_id, e.parent_node_id
        FROM edges e
        WHERE e.parent_node_id IN (
            SELECT n.node_id FROM nodes n
            JOIN boards_db.boards b ON n.board_id = b.id
            WHERE b.piece_count = ?
        )
    """, (L,))
    reverse = {}
    for c, p in cur.fetchall():
        if c in layer_nodes:
            reverse.setdefault(c, []).append(p)
    print(f"  {len(reverse):,} same-layer entries in {time.time()-t:.1f}s")

    # ---------- 6. Propagate in-memory ----------
    print(f"\n[6/6] Propagating...")
    t = time.time()
    processed = 0
    last_log = time.time()

    while queue:
        nid = queue.popleft()
        val = cur_values[nid]
        parents = reverse.get(nid)
        if not parents:
            continue

        for parent in parents:
            if parent in cur_values:
                continue
            wc = cur_wc.get(parent, 0)
            if val == 'LOSS':
                cur_values[parent] = 'WIN'
                cur_wc[parent] = wc
                queue.append(parent)
            elif val == 'WIN':
                wc += 1
                tc = total_children[parent]
                if tc > 0 and wc == tc:
                    cur_values[parent] = 'LOSS'
                    cur_wc[parent] = wc
                    queue.append(parent)
                else:
                    cur_wc[parent] = wc
            # DRAW: no action

        processed += 1
        if time.time() - last_log > 15:
            print(f"    {processed:,} processed, queue {len(queue):,}, "
                  f"elapsed {time.time()-t:.0f}s")
            last_log = time.time()

    print(f"  Propagation done in {time.time()-t:.1f}s "
          f"({processed:,} nodes processed)")

    # ---------- 7. Flush all values to DB ----------
    print(f"\nFlushing to DB...")
    t = time.time()
    cur.execute("DROP TABLE IF EXISTS temp_layer")
    cur.execute("CREATE TEMP TABLE temp_layer (node_id INTEGER PRIMARY KEY)")
    batch = [(nid,) for nid in layer_nodes]
    for i in range(0, len(batch), 10000):
        cur.executemany("INSERT INTO temp_layer VALUES (?)", batch[i:i+10000])
    conn.commit()

    # Mark remaining as DRAW first (any that were never valued)
    cur.execute("""
        UPDATE nodes SET value = 'DRAW', win_children = 0
        WHERE value IS NULL AND node_id IN (SELECT node_id FROM temp_layer)
    """)
    conn.commit()
    n_draws_final = cur.rowcount
    print(f"  Remaining NULL marked as DRAW: {n_draws_final:,}")

    # Now flush WIN/LOSS values
    updates = [(v, cur_wc.get(nid, 0), nid)
               for nid, v in cur_values.items()]
    print(f"  Flushing {len(updates):,} valued nodes...")
    for i in range(0, len(updates), FLUSH_BATCH):
        cur.executemany(
            "UPDATE nodes SET value = ?, win_children = ? WHERE node_id = ?",
            updates[i:i+FLUSH_BATCH])
    conn.commit()
    cur.execute("DROP TABLE temp_layer")
    conn.commit()
    print(f"  Flush done in {time.time()-t:.1f}s")

    # ---------- 8. Summary ----------
    print(f"\n[Summary] Layer {L} value distribution:")
    cur.execute("""
        SELECT value, COUNT(*)
        FROM nodes n
        JOIN boards_db.boards b ON n.board_id = b.id
        WHERE b.piece_count = ?
        GROUP BY value
    """, (L,))
    for v, c in cur.fetchall():
        print(f"    {v}: {c:,}")

    # Check start node if this is the final layer
    if L == 8:
        cur.execute("SELECT value FROM state WHERE key = 'start_node_id'")
        row = cur.fetchone()
        if row:
            start_nid = int(row[0])
            cur.execute("SELECT value FROM nodes WHERE node_id = ?", (start_nid,))
            sv = cur.fetchone()[0]
            print(f"\n{'*' * 72}")
            print(f"  *** START NODE VALUE: {sv} ***")
            if sv == 'WIN':
                print("  *** White has a winning strategy! ***")
            elif sv == 'LOSS':
                print("  *** White loses with perfect play. ***")
            else:
                print("  *** DRAW — White cannot force a win. ***")
            print(f"{'*' * 72}")

    print(f"\n  Layer {L} time: {time.time()-t0:.1f}s "
          f"({(time.time()-t0)/60:.1f} min)")

    cur.execute("DETACH DATABASE boards_db")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--layer', type=int, required=True)
    args = ap.parse_args()
    solve_layer(args.layer)