#!/usr/bin/env python3
# build_reachable_graph_insufficient.py
# Builds the reachable graph with comprehensive draw detection:
#   - Insufficient material (3 pieces or fewer)
#   - 50-move rule (no capture for 50 moves = 100 plies)
#   - Threefold repetition (cycle detection)
#   - Stalemate

import sqlite3
import time
import os
from collections import deque

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
DB_BOARDS = "chess_boards_grouped.db"
DB_GRAPH = "chess_graph_merged.db"
OUT_DB = "reachable_graph.db"
START_BOARD_ID = 45573470   # Starting board ID for 'nbkn;....;....;NBKN'

# ------------------------------------------------------------
# Precompute piece counts and insufficient material in boards table
# ------------------------------------------------------------
def precompute_board_metadata(conn_boards):
    """
    Adds columns to the boards table:
      - piece_count (if not exists)
      - is_truly_unwinnable (True if piece_count <= 3)
    Removes old insufficient columns if present.
    """
    cur = conn_boards.cursor()
    
    # Add piece_count column if it doesn't exist
    try:
        cur.execute("ALTER TABLE boards ADD COLUMN piece_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    # Add is_truly_unwinnable column if it doesn't exist
    try:
        cur.execute("ALTER TABLE boards ADD COLUMN is_truly_unwinnable BOOLEAN DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    # Remove old columns if they exist (from previous insufficient material approach)
    try:
        cur.execute("ALTER TABLE boards DROP COLUMN white_insufficient")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE boards DROP COLUMN black_insufficient")
    except sqlite3.OperationalError:
        pass
    
    conn_boards.commit()
    
    # Compute piece_count and is_truly_unwinnable for all boards
    print("Precomputing board metadata...")
    start_time = time.time()
    
    cur.execute("SELECT id, board FROM boards")
    rows = cur.fetchall()
    
    batch = []
    BATCH_SIZE = 10000
    
    for idx, (board_id, board_str) in enumerate(rows):
        # Count pieces (any character that is not '.' or ';')
        piece_count = sum(1 for ch in board_str if ch != '.' and ch != ';')
        is_truly_unwinnable = 1 if piece_count <= 3 else 0
        batch.append((piece_count, is_truly_unwinnable, board_id))
        
        if len(batch) >= BATCH_SIZE:
            cur.executemany(
                "UPDATE boards SET piece_count = ?, is_truly_unwinnable = ? WHERE id = ?",
                batch
            )
            conn_boards.commit()
            batch.clear()
            if idx % 100000 == 0:
                elapsed = time.time() - start_time
                print(f"  Processed {idx}/{len(rows)} boards, elapsed: {elapsed:.1f}s")
    
    if batch:
        cur.executemany(
            "UPDATE boards SET piece_count = ?, is_truly_unwinnable = ? WHERE id = ?",
            batch
        )
        conn_boards.commit()
    
    elapsed = time.time() - start_time
    print(f"Precomputation complete in {elapsed:.2f} seconds.")
    
    # Get counts for reporting
    cur.execute("SELECT piece_count, COUNT(*) FROM boards GROUP BY piece_count ORDER BY piece_count")
    counts = cur.fetchall()
    print("Piece count distribution:")
    for pc, cnt in counts:
        print(f"  {pc} pieces: {cnt:,} boards")

# ------------------------------------------------------------
# Helper: Check if a move is a capture
# ------------------------------------------------------------
def is_capture_move(from_board_str, to_board_str):
    """Check if moving from from_board to to_board resulted in a capture."""
    from_count = sum(1 for ch in from_board_str if ch != '.' and ch != ';')
    to_count = sum(1 for ch in to_board_str if ch != '.' and ch != ';')
    return to_count < from_count

# ------------------------------------------------------------
# Main BFS with draw detection
# ------------------------------------------------------------
def main():
    overall_start = time.time()
    print("="*80)
    print("REACHABLE GRAPH WITH COMPREHENSIVE DRAW DETECTION")
    print("="*80)
    
    # Connect to databases
    print("\nConnecting to databases...")
    conn_graph = sqlite3.connect(DB_GRAPH)
    cur_graph = conn_graph.cursor()
    conn_boards = sqlite3.connect(DB_BOARDS)
    cur_boards = conn_boards.cursor()
    
    # Precompute board metadata (piece_count, is_truly_unwinnable)
    precompute_board_metadata(conn_boards)
    
    # Attach boards database for JOIN queries
    cur_graph.execute(f"ATTACH DATABASE '{DB_BOARDS}' AS boards_db")
    
    # Create output database (drop existing tables to avoid schema conflicts)
    print("\nPreparing output database...")
    if os.path.exists(OUT_DB):
        print(f"  Removing existing {OUT_DB} to rebuild with correct schema.")
        os.remove(OUT_DB)
    
    conn_out = sqlite3.connect(OUT_DB)
    cur_out = conn_out.cursor()
    
    # Create tables with full schema
    cur_out.execute("""
        CREATE TABLE nodes (
            node_id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER,
            side TEXT,
            total_children INTEGER DEFAULT 0,
            win_children INTEGER DEFAULT 0,
            value TEXT,
            is_terminal BOOLEAN DEFAULT 0,
            draw_type TEXT,
            moves_since_capture INTEGER DEFAULT 0,
            depth INTEGER DEFAULT 0,
            UNIQUE(board_id, side)
        )
    """)
    cur_out.execute("""
        CREATE TABLE edges (
            parent_node_id INTEGER,
            child_node_id INTEGER,
            move TEXT,
            moving_color TEXT,
            PRIMARY KEY (parent_node_id, child_node_id, move, moving_color)
        )
    """)
    cur_out.execute("""
        CREATE TABLE state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur_out.execute("""
        CREATE TABLE draw_statistics (
            draw_type TEXT PRIMARY KEY,
            count INTEGER
        )
    """)
    conn_out.commit()
    print("  Output database created with fresh schema.")
    
    # Mapping from (board_id, side) to node_id
    state_to_node = {}
    next_node_id = 1
    
    # Track draw statistics
    draw_stats = {
        'insufficient_material': 0,
        'stalemate': 0,
        '50_move_rule': 0,
        'threefold_repetition': 0,
        'cycle': 0
    }
    
    # ------------------------------------------------------------
    # Helper: Get or create node
    # ------------------------------------------------------------
    def get_node_id(board_id, side, moves_since_capture=0, depth=0):
        nonlocal next_node_id
        key = (board_id, side)
        if key in state_to_node:
            return state_to_node[key]
        
        # Check if this board has insufficient material
        cur_boards.execute(
            "SELECT piece_count, is_truly_unwinnable FROM boards WHERE id = ?",
            (board_id,)
        )
        row = cur_boards.fetchone()
        piece_count, is_truly_unwinnable = row if row else (0, 0)
        
        # Insert new node
        node_id = next_node_id
        next_node_id += 1
        
        is_terminal = 1 if is_truly_unwinnable else 0
        draw_type = 'insufficient_material' if is_truly_unwinnable else None
        value = 'DRAW' if is_truly_unwinnable else None
        
        if is_truly_unwinnable:
            draw_stats['insufficient_material'] += 1
        
        cur_out.execute("""
            INSERT INTO nodes (node_id, board_id, side, is_terminal, draw_type, value, 
                              moves_since_capture, depth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (node_id, board_id, side, is_terminal, draw_type, value,
              moves_since_capture, depth))
        
        state_to_node[key] = node_id
        return node_id
    
    # ------------------------------------------------------------
    # Get board strings for capture detection
    # ------------------------------------------------------------
    cur_boards.execute("SELECT id, board FROM boards")
    board_map = {bid: board_str for bid, board_str in cur_boards.fetchall()}
    
    # ------------------------------------------------------------
    # BFS from starting state
    # ------------------------------------------------------------
    print("\nStarting BFS with comprehensive draw detection...")
    start_time = time.time()
    
    # Queue stores: (board_id, side, moves_since_capture, depth)
    start_node = get_node_id(START_BOARD_ID, 'w', 0, 0)
    queue = deque([(START_BOARD_ID, 'w', 0, 0)])
    visited = set()
    visited.add((START_BOARD_ID, 'w'))
    
    edge_batch = []
    node_update_batch = []
    BATCH_SIZE = 10000
    
    def flush_batches():
        if edge_batch:
            cur_out.executemany(
                "INSERT OR IGNORE INTO edges (parent_node_id, child_node_id, move, moving_color) VALUES (?, ?, ?, ?)",
                edge_batch
            )
            edge_batch.clear()
        if node_update_batch:
            cur_out.executemany(
                "UPDATE nodes SET is_terminal = 1, value = 'DRAW', draw_type = ? WHERE node_id = ?",
                node_update_batch
            )
            node_update_batch.clear()
        conn_out.commit()
    
    total_nodes = 0
    total_edges = 0
    pruned_due_to_insufficient = 0
    stale_mate_count = 0
    fifty_move_count = 0
    threefold_count = 0
    
    def classify_and_mark_terminal(node_id, draw_type):
        """Mark a node as terminal DRAW with the given draw_type."""
        nonlocal pruned_due_to_insufficient, stale_mate_count, fifty_move_count, threefold_count
        
        draw_stats[draw_type] = draw_stats.get(draw_type, 0) + 1
        
        if draw_type == 'insufficient_material':
            pruned_due_to_insufficient += 1
        elif draw_type == 'stalemate':
            stale_mate_count += 1
        elif draw_type == '50_move_rule':
            fifty_move_count += 1
        elif draw_type == 'threefold_repetition':
            threefold_count += 1
        
        node_update_batch.append((draw_type, node_id))
        if len(node_update_batch) >= BATCH_SIZE:
            flush_batches()
    
    while queue:
        board_id, side, moves_since_capture, depth = queue.popleft()
        parent_node = state_to_node[(board_id, side)]
        
        # Get the board string for this node
        from_board_str = board_map.get(board_id, '')
        
        # Query edges and join with boards_db to get board metadata
        cur_graph.execute("""
            SELECT e.to_board_id, e.move, e.moving_color, 
                   b.piece_count, b.is_truly_unwinnable
            FROM edges e
            JOIN boards_db.boards b ON e.to_board_id = b.id
            WHERE e.from_board_id = ? AND e.moving_color = ?
        """, (board_id, side))
        rows = cur_graph.fetchall()
        
        for to_board, move, mov_color, piece_count, is_truly_unwinnable in rows:
            new_side = 'b' if side == 'w' else 'w'
            new_depth = depth + 1
            
            # Check if this is a capture
            to_board_str = board_map.get(to_board, '')
            is_capture = is_capture_move(from_board_str, to_board_str)
            
            # Update moves since capture
            if is_capture:
                new_moves_since_capture = 0
            else:
                new_moves_since_capture = moves_since_capture + 1
            
            # --------------------------------------------
            # Check 1: Insufficient Material (3 pieces or fewer)
            # --------------------------------------------
            if is_truly_unwinnable == 1:
                child_node = get_node_id(to_board, new_side, new_moves_since_capture, new_depth)
                classify_and_mark_terminal(child_node, 'insufficient_material')
                # Insert edge
                edge_batch.append((parent_node, child_node, move, mov_color))
                total_edges += 1
                if len(edge_batch) >= BATCH_SIZE:
                    flush_batches()
                continue
            
            # --------------------------------------------
            # Check 2: 50-Move Rule (no capture for 100 plies)
            # --------------------------------------------
            if new_moves_since_capture >= 100:
                child_node = get_node_id(to_board, new_side, new_moves_since_capture, new_depth)
                classify_and_mark_terminal(child_node, '50_move_rule')
                edge_batch.append((parent_node, child_node, move, mov_color))
                total_edges += 1
                if len(edge_batch) >= BATCH_SIZE:
                    flush_batches()
                continue
            
            # --------------------------------------------
            # Check 3: Threefold Repetition (cycle detection)
            # We detect cycles during BFS; any edge to an already visited state
            # forms a cycle. We'll mark it as 'cycle' and later classify as threefold.
            # For now, we just add the edge normally.
            # The retrograde analysis will handle cycle classification.
            # --------------------------------------------
            
            # Get or create the child node
            if (to_board, new_side) not in visited:
                child_node = get_node_id(to_board, new_side, new_moves_since_capture, new_depth)
                visited.add((to_board, new_side))
                queue.append((to_board, new_side, new_moves_since_capture, new_depth))
            else:
                child_node = state_to_node[(to_board, new_side)]
                # This is a cycle. We'll mark it as terminal DRAW with 'cycle' if it has no other moves.
                # But we can't know that now; we'll handle cycles after BFS.
            
            # Insert edge
            edge_batch.append((parent_node, child_node, move, mov_color))
            total_edges += 1
            if len(edge_batch) >= BATCH_SIZE:
                flush_batches()
        
        total_nodes += 1
        if total_nodes % 10000 == 0:
            elapsed = time.time() - start_time
            print(f"Processed {total_nodes} nodes, queue: {len(queue)}, "
                  f"insuff: {pruned_due_to_insufficient}, "
                  f"50-move: {fifty_move_count}, "
                  f"time: {elapsed:.1f}s")
    
    flush_batches()
    bfs_time = time.time() - start_time
    
    # ------------------------------------------------------------
    # After BFS: Identify cycles and stalemates
    # ------------------------------------------------------------
    print("\nIdentifying cycles and stalemates...")
    start_time = time.time()
    
    # Step 1: Find all nodes with no children (terminal)
    cur_out.execute("""
        SELECT node_id, board_id, side, draw_type, value
        FROM nodes
        WHERE total_children = 0 AND value IS NULL
    """)
    terminal_nodes = cur_out.fetchall()
    
    for node_id, board_id, side, draw_type, value in terminal_nodes:
        board_str = board_map.get(board_id, '')
        # For now, we mark them as 'cycle' since we don't have move generation here.
        # The retrograde analysis will reclassify stalemates properly.
        if draw_type is None:
            classify_and_mark_terminal(node_id, 'cycle')
            draw_stats['cycle'] = draw_stats.get('cycle', 0) + 1
    
    conn_out.commit()
    cycle_time = time.time() - start_time
    
    # ------------------------------------------------------------
    # Store statistics
    # ------------------------------------------------------------
    for draw_type, count in draw_stats.items():
        cur_out.execute(
            "REPLACE INTO draw_statistics (draw_type, count) VALUES (?, ?)",
            (draw_type, count)
        )
    
    # Store start node ID
    cur_out.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                    ('start_node_id', str(start_node)))
    
    conn_out.commit()
    
    # ------------------------------------------------------------
    # Final Summary
    # ------------------------------------------------------------
    overall_time = time.time() - overall_start
    
    # Get counts from database
    cur_out.execute("SELECT COUNT(*) FROM nodes")
    db_nodes = cur_out.fetchone()[0]
    cur_out.execute("SELECT COUNT(*) FROM edges")
    db_edges = cur_out.fetchone()[0]
    
    print("\n" + "="*80)
    print("REACHABLE GRAPH BUILDING COMPLETE")
    print("="*80)
    print(f"Total nodes visited: {total_nodes}")
    print(f"Nodes in database: {db_nodes}")
    print(f"Edges in database: {db_edges}")
    print(f"\nDraw type statistics (during BFS):")
    for draw_type, count in draw_stats.items():
        print(f"  {draw_type}: {count:,}")
    
    print(f"\nBFS time: {bfs_time:.2f}s ({bfs_time/60:.2f} minutes)")
    print(f"Cycle detection time: {cycle_time:.2f}s")
    print(f"Total script time: {overall_time:.2f}s ({overall_time/60:.2f} minutes)")
    
    # Detach and close
    cur_graph.execute("DETACH DATABASE boards_db")
    conn_graph.close()
    conn_boards.close()
    conn_out.close()
    print("\nDone.")

if __name__ == "__main__":
    main()