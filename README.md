# 4x4 Mini-Chess Solver: Exhaustive Enumeration, Retrograde Analysis, and Strategy DAGs

## Project Overview

This repository contains the complete algorithmic pipeline, data generation scripts, and visualisation tools used to solve two 4x4 mini-chess variants under perfect play. 

The central research question is: **Does White have a winning strategy from the starting position when both sides play optimally?**

By exhaustively enumerating every legal board, building a complete move graph, detecting cycles, and applying layer-by-layer retrograde analysis, we definitively answer this question in the negative. 

**The primary result of this research is that both starting positions are DRAWs under perfect play. White does not have a winning strategy in either variant.**

Beyond the standard retrograde analysis, this repository includes two advanced, independent cross-validation techniques:
1. **Algorithm B (White's Winning Set):** An independent pre-check that proves White cannot win in ~10 minutes (as opposed to ~2.5 hours for the full retrograde).
2. **Strategy DAG (AND-OR Tree):** A 5.9-million-node playable witness that proves Black can force a draw against *any* White strategy, validated with 17 independent checks.

---

## ⚠️ IMPORTANT: Database Storage & Generation

**The SQLite databases are NOT included in this repository.** 

Because the search is exhaustive, the generated databases are extremely large (approximately **150–200 GB per variant**). GitHub does not support files of this size. 

To replicate the experiments, **you must run the generation scripts locally**. The algorithms are designed to be completely reproducible. You will need at least **250 GB of free disk space** before starting.

---

## Repository Structure

The code is split into two main folders (`variant_A/` and `variant_B/`) because the piece sets, starting positions, and resulting graph sizes are different. A shared `visualisation/` folder contains the HTML/JS dashboards.

```text
/
├── variant_A/               # Scripts and analysis for NB2K/nb2k (1 Bishop, 2 Knights)
├── variant_B/               # Scripts and analysis for NBBK/kbbn (2 Bishops, 1 Knight)
├── visualisation/           # HTML/JS dashboards, cloud visualisers, and the playable game
└── README.md
