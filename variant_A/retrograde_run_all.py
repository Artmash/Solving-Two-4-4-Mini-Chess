#!/usr/bin/env python3
# run_all_retrograde.py
# Runs retrograde layer-by-layer, bottom-up: 3 → 4 → 5 → 6 → 7 → 8.
# Checks after layer 8 whether White wins.

import subprocess
import sys
import time

LAYERS = [3, 4, 5, 6, 7, 8]

def main():
    overall = time.time()
    print("=" * 72)
    print("RETROGRADE — ALL LAYERS (BOTTOM-UP)")
    print("=" * 72)

    for L in LAYERS:
        print(f"\n{'#' * 72}")
        print(f"# LAYER {L}")
        print(f"{'#' * 72}")
        t = time.time()
        r = subprocess.run(
            [sys.executable, "retrograde_layer.py", "--layer", str(L)])
        print(f"\nLayer {L}: exit code {r.returncode}, "
              f"time {time.time()-t:.1f}s")

    print("\n" + "=" * 72)
    print(f"Total retrograde time: {time.time()-overall:.1f}s "
          f"({(time.time()-overall)/60:.1f} min)")
    print("=" * 72)


if __name__ == "__main__":
    main()