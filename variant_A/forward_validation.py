#!/usr/bin/env python3
# validate_consistency.py
# Validates retrograde analysis using minimax consistency check.

import sqlite3
import random
import sys

DB_GRAPH = "reachable_graph.db"
SAMPLE_SIZE = 1000000  # can process many quickly

def main():
    conn = sqlite3.connect(DB_GRAPH)
    cur = conn.cursor()

    # Get all nodes with values (skip NULL)
    cur.execute("SELECT node_id, value FROM nodes WHERE value IS NOT NULL")
    all_nodes = cur.fetchall()
    print(f"Total nodes with values: {len(all_nodes)}")

    # Sample random nodes
    sample = random.sample(all_nodes, min(SAMPLE_SIZE, len(all_nodes)))
    print(f"Sample size: {len(sample)}")

    matched = 0
    mismatched = 0
    errors = 0

    # For each sampled node, compute the minimax value from its children
    for node_id, stored_value in sample:
        # Get children values
        cur.execute("""
            SELECT n.value
            FROM edges e
            JOIN nodes n ON e.child_node_id = n.node_id
            WHERE e.parent_node_id = ? AND n.value IS NOT NULL
        """, (node_id,))
        child_values = [row[0] for row in cur.fetchall()]

        if not child_values:
            # Terminal node: value should be LOSS or DRAW
            if stored_value in ('LOSS', 'DRAW'):
                matched += 1
            else:
                mismatched += 1
                print(f"Mismatch: node {node_id} stored {stored_value} but terminal should be LOSS/DRAW")
            continue

        # For the player to move, if any child is LOSS, node is WIN
        # If all children are WIN, node is LOSS
        # Otherwise DRAW
        if 'LOSS' in child_values:
            computed = 'WIN'
        elif all(v == 'WIN' for v in child_values):
            computed = 'LOSS'
        else:
            computed = 'DRAW'

        if computed == stored_value:
            matched += 1
        else:
            mismatched += 1
            print(f"Mismatch: node {node_id} stored {stored_value} but computed {computed} from children")

        if (matched + mismatched) % 10000 == 0:
            print(f"Processed {matched + mismatched} nodes...")

    print("\n" + "="*80)
    print("VALIDATION RESULTS")
    print("="*80)
    print(f"Total nodes processed: {len(sample)}")
    print(f"Matched: {matched}")
    print(f"Mismatched: {mismatched}")
    print(f"Errors: {errors}")
    if mismatched == 0 and errors == 0:
        print("\n✓ Consistency check PASSED. All minimax rules are satisfied.")
    else:
        print("\n✗ Validation FAILED. Mismatches or errors detected.")
        print("  Please investigate the discrepancies.")

    conn.close()

if __name__ == "__main__":
    main()