# 4×4 Mini Chess Solver

## Exhaustive Board Enumeration, Move Graph Construction, and Retrograde Analysis

**Research focus:** determining the game theoretic outcome of two 4×4
mini chess variants under perfect play.

This document describes the core computational research pipeline,
beginning with legal board generation and ending with retrograde
analysis of every position reachable from the specified starting
position. It covers the graph construction, reachability reduction,
cycle analysis, treatment of the 50 move rule, and validation of the
computed minimax values.

The central research question is:

> **Does White have a winning strategy from the starting position when
> both players play optimally?**

The computed result is **DRAW for both variants**. Therefore, under the
rules and modelling assumptions implemented by this project, White
cannot force a win from either starting position against optimal Black
play.

## 1. Research overview

The solver treats each legal board configuration as a state in a
directed game graph.

The pipeline is designed to be reproducible. The large SQLite databases
are generated locally and are not committed to the repository.

### Main computational stages

1.  Enumerate legal board configurations.
2.  Generate legal move edges between configurations.
3.  Extract the graph reachable from the starting position.
4.  Detect strongly connected components and classify cycles.
5.  Check whether the 50 move rule can affect the explored games.
6.  Run layer by layer retrograde analysis.
7.  Validate the resulting minimax classifications.

## 2. The two mini chess variants

The repository separates the variants into `variant_A/` and
`variant_B/`, because their piece sets, initial positions, and
state space sizes differ.

### Variant A NB2K / `nb2k`

**Piece set:** Each side has one king, one bishop, and two knights.

**Starting position:**

```text
nbkn
....
....
NBKN
```

**Computed reachable graph statistics:**

    Reachable nodes: 49,058,772
    Reachable edges: 288,073,820
    WIN positions: 21.23%
    LOSS positions: 6.85%
    DRAW positions: 71.92%
    Maximum observed depth: 27 plies

### Variant B NBBK / `kbbn`

**Piece set:** Each side has one king, two bishops (one
light square bound and one dark square bound), and one knight.

**Starting position:**

```text
NBBK
....
....
kbbn
```

**Computed reachable graph statistics:**

    Reachable nodes: 55,590,352
    Reachable edges: 324,018,512
    WIN positions: 25.69%
    LOSS positions: 7.95%
    DRAW positions: 66.36%
    Maximum observed depth: 31 plies

## 3. Repository structure

```text
/
├── variant_A/
│   ├── board_generator_parallel.py
│   ├── phase1_edges_parallel_simple.py.py
│   ├── build_reachable_graph_insufficient.py
│   ├── detect_cycles_scc.py
│   ├── 50_move_rule.py
│   ├── retrograde_layer.py
│   ├── run_all_retrograde.py
│   ├──  forward_validation.py
    └──  build_draw_strategy_tree
│
├── variant_B/
│   ├── board_generator_new_variant.py
│   ├── edge_generator_new_variant.py
│   ├── build_reachable_graph_new_variant.py
│   ├── detect_cycles_scc.py
│   ├── apply_50_move_rule.py
│   ├── retrograde_layer.py
│   ├── run_all_retrograde.py
│   ├── validate_minimax.py
│   ├── build_black_strategy_tree.py
│   └── validate_black_strategy_tree.py
│
└── README.md
```

The first seven scripts form the principal generation and solving
workflow.

## 4. Requirements and resource considerations

### Software

    Python 3.10 or later
    SQLite 3
    NumPy
    SciPy

Install the Python dependencies:

```bash
pip install numpy scipy
```

### Hardware and storage

The state space is large, so the experiment is resource intensive.

    Recommended CPU: at least 4 cores
    Recommended RAM: at least 16 GB
    Free disk space: allow at least **250 GB** before beginning a full
    run
    Approximate generated database size: **150  200 GB per variant**
    Approximate full pipeline duration: around **72 hours per variant**
    on the stated reference hardware

Actual runtime and storage use depend on the hardware, SQLite
configuration, available memory, and disk performance.

### Generated databases are not included

The SQLite databases are intentionally excluded from the repository
because of their size. To reproduce the experiment, run the generation
scripts locally and retain the generated databases between phases.

## 5. Phase 1 Exhaustive legal board enumeration

### Script

```text
board_generator.py
```

### Purpose

The first phase generates the set of legal board configurations for the
selected variant. This establishes the state universe from which the
move graph will be constructed.

### Method

The board generator systematically iterates through possible piece
placements, rejects configurations that violate the legality
constraints, and stores valid configurations in SQLite.

Generation is parallelised across multiple worker processes. A resume
mechanism allows an interrupted generation run to continue without
intentionally restarting the entire enumeration.

### Output

```text
chess_boards_grouped.db
```

The database contains the `boards` table, which stores the generated
legal configurations and their identifiers.

### Run

```bash
python board_generator.py   workers 4
```

## 6. Phase 2 Legal move generation and graph construction

### Script

```text
edge_generator.py
```

### Purpose

Once the legal board database exists, the edge generator constructs the
directed move graph. For each stored board, it generates the legal moves
available to the player whose turn it is and records the corresponding
successor positions.

In graph terms:

\[ G = (V,E) \]

where:

    \(V\) is the set of enumerated legal board states.
    \(E\) is the set of directed legal move transitions.

An edge ((u,v)) means that a legal move takes the game from state (u) to
state (v).

### Input

```text
chess_boards_grouped.db
```

### Output

```text
chess_graph_merged.db
```

The output contains an `edges` table representing the generated move
transitions.

### Run

```bash
python edge_generator.py   workers 4   chunks 8
```

## 7. Phase 3 Extracting the reachable game graph

### Script

```text
build_reachable_grap.py
```

### Purpose

The exhaustive board database contains legal configurations that may
never occur in a game starting from the selected initial position. The
reachability phase extracts only the positions that can actually be
reached from that starting position by following legal moves.

This produces a smaller, experiment specific graph:

\[ G_R = (V_R,E_R) \]

where (V_R) contains states reachable from the initial position and
(E_R) contains the legal transitions between those reachable states.

### Input

```text
chess_boards_grouped.db
chess_graph_merged.db
```

### Output

```text
reachable_graph.db
```

The database contains:

    `nodes`     reachable position records.
    `edges`     legal transitions between reachable positions.
    `state`     state and outcome metadata used by subsequent analysis.

### Run

```bash
python build_reachable_graph.py
```

## 8. Phase 4 Cycle detection and strongly connected components

### Script

```text
detect_cycles_scc.py
```

### Purpose

A game graph may contain cycles. A player can sometimes move between
positions without capturing, returning to a previously visited state.
Such cycles matter because a game may continue indefinitely unless a
draw rule or another terminal condition applies.

### Input

```text
reachable_graph.db
```

### Output

```text
cycle_analysis_scc.db
```

The output contains an `sccs` table with component membership and
classification information.

### Run

```bash
python detect_cycles_scc.py
```

## 9. Phase 5 Checking the 50 move rule

### Script

```text
apply_50_move_rule.py
```

### Purpose

The FIDE 50 move rule allows a draw to be claimed after 50 moves by each
player without a pawn move or capture. In a game state representation
that counts individual plies, this corresponds to 100 consecutive
half moves without a capture or pawn move.

The project checks whether this rule can affect the explored games.

### Input

```text
reachable_graph.db
```

### Output

The script updates:

```text
reachable_graph.db
```

### Run

```bash
python apply_50_move_rule.py
```

## 10. Phase 6 Layer by layer retrograde analysis

### Scripts

```text
retrograde_layer.py
run_all_retrograde.py
```

### Purpose

Retrograde analysis is the central solving algorithm. It determines
whether each reachable position is a WIN, LOSS, or DRAW under optimal
play.

The analysis works backwards from known terminal positions. Instead of
simulating every possible complete game, it uses the structure of the
directed move graph to infer the value of predecessor positions.

### Inputs

```text
reachable_graph.db
```

### Outputs

The scripts update:

```text
reachable_graph.db
```

The database stores the computed `value` and `win_children` information
used to represent the outcome of each position and the moves supporting
a win.

### Run

Run the complete layer sequence:

```bash
python run_all_retrograde.py
```

The driver invokes `retrograde_layer.py` sequentially for the
piece count layers from three through eight.

## 11. Phase 7 Minimax consistency validation

### Script

```text
validate_minimax.py
```

### Purpose

The validation script checks whether stored node values are consistent
with the minimax rules applied to their successor positions.

It samples nodes from the reachable graph, recomputes the expected value
from the children's stored values, and compares that expectation with
the value stored by retrograde analysis.

### Input

```text
reachable_graph.db
```

### Run

Variant A example:

```bash
python validate_minimax.py   sample 1000000
```

Variant B example:

```bash
python validate_minimax.py   sample 6000000
```

## 12. Research conclusion

This project applies exhaustive state enumeration, directed move graph
construction, reachability analysis, SCC based cycle analysis, and
layer by layer retrograde computation to two 4×4 mini chess variants.

The resulting reachable graphs contain tens of millions of positions and
hundreds of millions of legal move edges. The solver classifies every
reachable position according to the implemented terminal, capture,
insufficient material, and draw rule assumptions.

The reported initial position outcome is:

    **Variant A (NB2K): DRAW**
    **Variant B (NBBK): DRAW**

Therefore, the research finds that **White does not have a forced win
from the starting position in either variant when both players play
optimally**.

The most important contribution of the core pipeline is the end to end
computational method: generate the legal state space, construct the move
graph, isolate the reachable game, analyse its cyclic structure, and
propagate game theoretic values backwards until the starting position is
resolved.

## 13. Citation and licence

If you use the code or results in your own work, cite:

> Mashimbyi, V. (2026). _Solving Two 4×4 Mini Chess Variants: Exhaustive
> Enumeration, Retrograde Analysis, Cycle Detection, and Independent
> Draw Strategy Certificates_. COS700 Research Report, University of
> Pretoria.
