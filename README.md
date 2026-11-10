# 4×4 Mini-Chess Solver

## Exhaustive Board Enumeration, Move-Graph Construction, and Retrograde Analysis

**Research focus:** determining the game-theoretic outcome of two 4×4
mini-chess variants under perfect play.

This document describes the core computational research pipeline,
beginning with legal-board generation and ending with retrograde
analysis of every position reachable from the specified starting
position. It covers the graph construction, reachability reduction,
cycle analysis, treatment of the 50-move rule, and validation of the
computed minimax values.

The central research question is:

> **Does White have a winning strategy from the starting position when
> both players play optimally?**

The computed result is **DRAW for both variants**. Therefore, under the
rules and modelling assumptions implemented by this project, White
cannot force a win from either starting position against optimal Black
play.

------------------------------------------------------------------------

## 1. Research overview

The solver treats each legal board configuration as a state in a
directed game graph.

-   A **node** represents a legal board position, including the side to
    move where that information is part of the state representation.
-   A directed **edge** represents one legal move from a position to its
    successor.
-   Terminal positions are classified according to the game rules,
    principally checkmate and stalemate.
-   Captures move play into a lower piece-count layer. Since captures
    reduce the number of pieces, the graph can be analysed in layers
    ordered by piece count.
-   Positions that can be reached from the specified initial position
    are extracted to form the reachable game graph.
-   Retrograde analysis propagates WIN and LOSS values backwards from
    known terminal positions. Positions that are neither proven WIN nor
    proven LOSS are classified as DRAW, subject to the cycle and rule
    handling described below.

The pipeline is designed to be reproducible. The large SQLite databases
are generated locally and are not committed to the repository.

### Main computational stages

1.  Enumerate legal board configurations.
2.  Generate legal move edges between configurations.
3.  Extract the graph reachable from the starting position.
4.  Detect strongly connected components and classify cycles.
5.  Check whether the 50-move rule can affect the explored games.
6.  Run layer-by-layer retrograde analysis.
7.  Validate the resulting minimax classifications.

------------------------------------------------------------------------

## 2. The two mini-chess variants

The repository separates the variants into `variant_A/` and
`variant_B/`, because their piece sets, initial positions, and
state-space sizes differ.

### Variant A --- NB2K / `nb2k`

**Piece set:** Each side has one king, one bishop, and two knights.

**Starting position:**

``` text
nbkn
....
....
NBKN
```

**Computed reachable-graph statistics:**

-   Reachable nodes: **49,058,772**
-   Reachable edges: **288,073,820**
-   WIN positions: **21.23%**
-   LOSS positions: **6.85%**
-   DRAW positions: **71.92%**
-   Maximum observed depth: **27 plies**
-   Strongly connected components (SCCs): **27**
-   CLOSED SCCs: **3**

### Variant B --- NBBK / `kbbn`

**Piece set:** Each side has one king, two bishops (one
light-square-bound and one dark-square-bound), and one knight.

**Starting position:**

``` text
NBBK
....
....
kbbn
```

**Computed reachable-graph statistics:**

-   Reachable nodes: **55,590,352**
-   Reachable edges: **324,018,512**
-   WIN positions: **25.69%**
-   LOSS positions: **7.95%**
-   DRAW positions: **66.36%**
-   Maximum observed depth: **31 plies**
-   Strongly connected components (SCCs): **81**
-   CLOSED SCCs: **0**

> **Notation:** The position strings above preserve the project's
> supplied starting-position notation. The implementation's board parser
> and piece-case convention are authoritative when reproducing the
> experiment.

------------------------------------------------------------------------

## 3. Repository structure

``` text
/
├── variant_A/
│   ├── board_generator_new_variant.py
│   ├── edge_generator_new_variant.py
│   ├── build_reachable_graph_new_variant.py
│   ├── detect_cycles_scc.py
│   ├── apply_50_move_rule.py
│   ├── retrograde_layer.py
│   ├── run_all_retrograde.py
│   └── validate_minimax.py
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
│   ├── precheck_not_white_win.py
│   ├── build_black_strategy_tree.py
│   └── validate_black_strategy_tree.py
│
└── README.md
```

The first seven scripts form the principal generation-and-solving
workflow. Variant B also contains additional independent
cross-validation scripts, described in Section 10.

------------------------------------------------------------------------

## 4. Requirements and resource considerations

### Software

-   Python 3.10 or later
-   SQLite 3
-   NumPy
-   SciPy

Install the Python dependencies:

``` bash
pip install numpy scipy
```

### Hardware and storage

The state space is large, so the experiment is resource-intensive.

-   Recommended CPU: at least 4 cores
-   Recommended RAM: at least 16 GB
-   Free disk space: allow at least **250 GB** before beginning a full
    run
-   Approximate generated database size: **150--200 GB per variant**
-   Approximate full pipeline duration: around **24 hours per variant**
    on the stated reference hardware

Actual runtime and storage use depend on the hardware, SQLite
configuration, available memory, and disk performance.

### Generated databases are not included

The SQLite databases are intentionally excluded from the repository
because of their size. To reproduce the experiment, run the generation
scripts locally and retain the generated databases between phases.

Do not begin a full run unless sufficient disk space is available. The
scripts should be run from inside the relevant variant directory.

------------------------------------------------------------------------

## 5. Phase 1 --- Exhaustive legal-board enumeration

### Script

``` text
board_generator_new_variant.py
```

### Purpose

The first phase generates the set of legal board configurations for the
selected variant. This establishes the state universe from which the
move graph will be constructed.

The generator enumerates piece placements subject to the constraints
encoded by the project. These include:

-   The required piece inventory for the selected variant.
-   Exactly one king per side.
-   Kings may not occupy adjacent squares.
-   Bishops remain bound to their respective square colours.
-   The permitted knight count is respected.
-   A piece may not occupy a square already occupied by another piece.
-   Other legality constraints implemented by the generator.

The precise legality rules are defined by the source code. When
reproducing or extending the experiment, the generator should be treated
as the authoritative definition of the enumerated state space.

### Method

The board generator systematically iterates through possible piece
placements, rejects configurations that violate the legality
constraints, and stores valid configurations in SQLite.

Generation is parallelised across multiple worker processes. A resume
mechanism allows an interrupted generation run to continue without
intentionally restarting the entire enumeration.

### Input

No previously generated database is required.

### Output

``` text
chess_boards_grouped.db
```

The database contains the `boards` table, which stores the generated
legal configurations and their identifiers.

### Run

From inside `variant_A/` or `variant_B/`:

``` bash
python board_generator_new_variant.py --workers 4
```

### Why this phase matters

Exhaustive enumeration is the foundation of the solver. If legal states
are omitted, or illegal states are admitted, later graph construction
and game-theoretic classification may be incorrect. The constraints and
state encoding must therefore be consistent across the generator, move
generator, and reachability builder.

------------------------------------------------------------------------

## 6. Phase 2 --- Legal move generation and graph construction

### Script

``` text
edge_generator_new_variant.py
```

### Purpose

Once the legal board database exists, the edge generator constructs the
directed move graph. For each stored board, it generates the legal moves
available to the player whose turn it is and records the corresponding
successor positions.

In graph terms:

\[ G = (V,E) \]

where:

-   \(V\) is the set of enumerated legal board states.
-   \(E\) is the set of directed legal-move transitions.

An edge ((u,v)) means that a legal move takes the game from state (u) to
state (v).

### Method

The script processes the board database in chunks and uses parallel
workers to improve throughput. For each position, it:

1.  Reads the source board.
2.  Determines the side to move.
3.  Generates that side's legal moves according to the variant's
    movement and legality rules.
4.  Encodes each resulting position.
5.  Looks up the destination position's database identifier.
6.  Stores the source-to-destination transition.

The implementation uses a dual-cursor approach to reduce database
locking problems during lookup and insertion. Work is divided into
chunks so that the graph can be generated in manageable units.

### Input

``` text
chess_boards_grouped.db
```

### Output

``` text
chess_graph_merged.db
```

The output contains an `edges` table representing the generated move
transitions.

### Run

``` bash
python edge_generator_new_variant.py --workers 4 --chunks 8
```

### Why this phase matters

The edge database represents the rules of play as a graph. A missing
edge can incorrectly remove a legal choice; an extra edge can give a
player an illegal choice. Either error can change the result of minimax
analysis. The move generator must therefore use the same state encoding
and legality assumptions as the board generator.

------------------------------------------------------------------------

## 7. Phase 3 --- Extracting the reachable game graph

### Script

``` text
build_reachable_graph_new_variant.py
```

### Purpose

The exhaustive board database contains legal configurations that may
never occur in a game starting from the selected initial position. The
reachability phase extracts only the positions that can actually be
reached from that starting position by following legal moves.

This produces a smaller, experiment-specific graph:

\[ G_R = (V_R,E_R) \]

where (V_R) contains states reachable from the initial position and
(E_R) contains the legal transitions between those reachable states.

### Method

The script performs a Breadth-First Search (BFS), beginning at the
variant's starting position.

The process is:

1.  Locate the starting board in the generated board database.
2.  Insert it into the BFS queue and mark it as visited.
3.  Remove a state from the queue.
4.  Retrieve its legal outgoing edges.
5.  Classify terminal states according to the game's rules.
6.  Add previously unseen successor states to the queue.
7.  Continue until no reachable states remain to be explored.

The implementation also applies the project's insufficient-material
treatment: branches that reach positions with three or fewer pieces are
marked as draws. Checkmate positions are classified as losses for the
player whose turn it is; stalemate positions are classified as draws.

These terminal and insufficient-material conventions are part of the
experiment's rules model and should be checked against the intended
mini-chess rules before comparing results with another implementation.

### Input

``` text
chess_boards_grouped.db
chess_graph_merged.db
```

### Output

``` text
reachable_graph.db
```

The database contains:

-   `nodes` --- reachable position records.
-   `edges` --- legal transitions between reachable positions.
-   `state` --- state and outcome metadata used by subsequent analysis.

### Run

``` bash
python build_reachable_graph_new_variant.py
```

### Why this phase matters

Retrograde analysis should answer the research question for the
specified initial position, not for every theoretically legal board
configuration. Restricting the graph to reachable states avoids spending
time solving disconnected positions that cannot influence the starting
position's outcome.

------------------------------------------------------------------------

## 8. Phase 4 --- Cycle detection and strongly connected components

### Script

``` text
detect_cycles_scc.py
```

### Purpose

A game graph may contain cycles. A player can sometimes move between
positions without capturing, returning to a previously visited state.
Such cycles matter because a game may continue indefinitely unless a
draw rule or another terminal condition applies.

A simple backward propagation from terminal positions is not, by itself,
sufficient to explain every cyclic region. The solver therefore analyses
strongly connected components (SCCs).

A strongly connected component is a maximal set of nodes in which every
node is reachable from every other node by following directed edges.

### Method

The implementation uses the fact that captures permanently reduce the
number of pieces. Consequently, a cycle cannot move across piece-count
layers: a path containing a capture cannot return to its original piece
count.

The script applies SciPy's Tarjan-based strongly connected component
routine separately to each piece-count layer, covering layers with three
through eight pieces.

Each SCC is classified according to its outgoing escape possibilities:

-   **CLOSED** --- no outgoing escape from the component.
-   **WHITE_ESCAPE** --- an escape is available to White.
-   **BLACK_ESCAPE** --- an escape is available to Black.
-   **BOTH_ESCAPE** --- escapes are available to both sides.

### Input

``` text
reachable_graph.db
```

### Output

``` text
cycle_analysis_scc.db
```

The output contains an `sccs` table with component membership and
classification information.

### Run

``` bash
python detect_cycles_scc.py
```

### Results

  Metric                            Variant A   Variant B
  ------------------------------- ----------- -----------
  Strongly connected components            27          81
  CLOSED components                         3           0

Variant A contains three closed/trapped SCCs. Variant B contains no
closed SCCs: every detected cyclic component has an escape according to
the component classification.

### Why this phase matters

Cycle analysis helps distinguish positions that can be resolved by
finite backward propagation from positions whose outcomes depend on the
possibility of continued play. It also provides structural information
about the graph, rather than treating all unresolved states as an
unexplained remainder.

------------------------------------------------------------------------

## 9. Phase 5 --- Checking the 50-move rule

### Script

``` text
apply_50_move_rule.py
```

### Purpose

The FIDE 50-move rule allows a draw to be claimed after 50 moves by each
player without a pawn move or capture. In a game-state representation
that counts individual plies, this corresponds to 100 consecutive
half-moves without a capture or pawn move.

The project checks whether this rule can affect the explored games.

### Method

The script performs a BFS while tracking the number of moves since the
most recent capture. It records the maximum counter reached in the
reachable state space.

### Input

``` text
reachable_graph.db
```

### Output

The script updates:

``` text
reachable_graph.db
```

### Run

``` bash
python apply_50_move_rule.py
```

### Result

The maximum counter reported for the two variants is **31 plies**, below
the 100-half-move threshold. Under the project's tracked-state
assumptions, the 50-move rule is therefore inert for both variants and
does not change the reported outcomes.

> The implementation should be checked to ensure that every
> rule-relevant event is tracked. In standard chess, the 50-move counter
> resets after either a pawn move or a capture. If the mini-chess
> variants contain no pawns, captures are the relevant reset event.

------------------------------------------------------------------------

## 10. Phase 6 --- Layer-by-layer retrograde analysis

### Scripts

``` text
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

### Game-theoretic definitions

For a position (v), let (Children(v)) be the set of positions reachable
from (v) in one legal move.

The solver uses the following minimax principles:

**WIN:** A position is a WIN for the player to move if that player has
at least one move to a position that is a LOSS for the opponent.

\[ v `\in `{=tex}WIN `\iff `{=tex}`\exists `{=tex}u
`\in `{=tex}Children(v): u `\in `{=tex}LOSS \]

**LOSS:** A position is a LOSS for the player to move if every legal
move leads to a position that is a WIN for the opponent.

\[ v `\in `{=tex}LOSS `\iff `{=tex}Children(v)
`\neq `{=tex}`\varnothing`{=tex} `\land`{=tex} `\forall `{=tex}u
`\in `{=tex}Children(v): u `\in `{=tex}WIN \]

**DRAW:** A position is a DRAW if neither player can force a win under
the modelled rules. This includes unresolved cyclic positions and
positions explicitly classified as draws, such as stalemate and the
project's insufficient-material terminals.

The exact interpretation of WIN and LOSS is relative to the side to move
at each node. A move to a child LOSS is favourable to the player making
the move; a move to a child WIN is unfavourable.

### Why analyse by piece-count layer?

Captures reduce the number of pieces and cannot be reversed. This gives
the graph a useful layered structure.

The solver processes piece-count layers from three through eight. When
solving a layer, it can use already-computed results from
lower-piece-count layers for transitions caused by captures.

This reduces the problem to a sequence of smaller layer-specific
retrograde computations.

### Algorithm

The implementation follows this general procedure for each layer:

1.  Identify terminal positions whose values are already known.
2.  Seed the propagation queue with those known positions.
3.  Pre-compute contributions from successor states in lower piece-count
    layers.
4.  Propagate values backwards through predecessor edges.
5.  If a node has a child classified as LOSS, classify the node as WIN.
6.  If all legal children of a node are classified as WIN, classify the
    node as LOSS.
7.  Leave positions that cannot be proven WIN or LOSS as DRAW, in
    accordance with the cycle and terminal-state handling.
8.  Store the resulting value and relevant winning-child information.

The central propagation rules are:

-   **Child is LOSS:** the parent can choose that move, so the parent is
    WIN.
-   **All children are WIN:** every move loses for the player to move,
    so the parent is LOSS.
-   **Neither condition is established:** the position remains
    unresolved and is ultimately treated as DRAW under the solver's draw
    model.

### Inputs

``` text
reachable_graph.db
```

### Outputs

The scripts update:

``` text
reachable_graph.db
```

The database stores the computed `value` and `win_children` information
used to represent the outcome of each position and the moves supporting
a win.

### Run

Run the complete layer sequence:

``` bash
python run_all_retrograde.py
```

The driver invokes `retrograde_layer.py` sequentially for the
piece-count layers from three through eight.

### Why this phase matters

This is the main computation that answers the research question. Once
the values have been propagated through the reachable graph, the value
stored for the initial position gives the result under optimal play:

-   Initial position WIN: the player to move can force a win.
-   Initial position LOSS: the opponent can force a win.
-   Initial position DRAW: neither side can force a win against optimal
    opposition.

For both variants, the reported initial-position result is **DRAW**.

------------------------------------------------------------------------

## 11. Phase 7 --- Minimax consistency validation

### Script

``` text
validate_minimax.py
```

### Purpose

The validation script checks whether stored node values are consistent
with the minimax rules applied to their successor positions.

It samples nodes from the reachable graph, recomputes the expected value
from the children's stored values, and compares that expectation with
the value stored by retrograde analysis.

### Method

For each sampled position, the validator checks the relevant conditions:

-   A node with at least one LOSS child should be WIN.
-   A node whose legal children are all WIN should be LOSS.
-   A node not satisfying either decisive condition should be consistent
    with the solver's DRAW classification and terminal/cycle treatment.

The project reports a sample size of one million nodes for Variant A and
six million nodes for Variant B, with a 100% match rate.

### Input

``` text
reachable_graph.db
```

### Run

Variant A example:

``` bash
python validate_minimax.py --sample 1000000
```

Variant B example:

``` bash
python validate_minimax.py --sample 6000000
```

### Interpreting the result

A 100% match means that every sampled node passed the validator's
consistency checks. This is strong evidence that the stored values agree
with the implemented minimax recurrence on the sampled positions.

It should not be described as a formal proof that the entire
implementation is bug-free: sampling cannot independently establish that
board generation, move generation, state encoding, or every database
operation is correct. The validation is most useful when combined with
code review, reproducible runs, structural checks, and independent
verification.

------------------------------------------------------------------------

## 12. Main experimental results

### Reachable graph and outcome distribution

  Metric                                  Variant A --- NB2K   Variant B --- NBBK
  ------------------------------------- -------------------- --------------------
  Legal boards                                    49,850,300           61,660,012
  Reachable nodes                                 49,058,772           55,590,352
  Reachable edges                                288,073,820          324,018,512
  WIN positions                                       21.23%               25.69%
  LOSS positions                                       6.85%                7.95%
  DRAW positions                                      71.92%               66.36%
  Maximum depth                                     27 plies             31 plies
  SCCs                                                    27                   81
  CLOSED SCCs                                              3                    0
  Maximum moves-since-capture counter               31 plies             31 plies

The outcome percentages describe the distribution of classified
reachable positions. They are not probabilities that a player wins or
draws from the starting position.

### Starting-position result

  Variant              Starting position       Result under perfect play
  -------------------- ----------------------- ---------------------------
  Variant A --- NB2K   `nbkn;....;....;NBKN`   **DRAW**
  Variant B --- NBBK   `NBBK;....;....;kbbn`   **DRAW**

The key result is not merely that many positions are classified as
draws. The initial position in each variant is itself classified as a
draw by the completed solver.

### Additional path observations

The project reports the following special-path measurements:

  Observation            Variant A   Variant B
  -------------------- ----------- -----------
  Shortest draw path       7 plies     6 plies
  Longest draw path       18 plies    24 plies

These figures should be interpreted according to the path definitions
and extraction procedure used by the corresponding analysis scripts. In
particular, a maximum path length in a cyclic graph needs a clearly
defined stopping condition or path convention.

------------------------------------------------------------------------

## 13. Variant B --- Additional independent cross-validation

The following scripts are additional checks for Variant B. They are not
required to understand the primary generation-to-retrograde pipeline,
but they provide further evidence for the reported result.

### 13.1 White-winning-set pre-check

**Script:**

``` text
precheck_not_white_win.py
```

This script computes the least fixed point of White's winning set,
denoted (W).

It starts from positions in which Black is checkmated and propagates
backwards:

-   A White-to-move position enters (W) if at least one child is already
    in (W).
-   A Black-to-move position enters (W) only if all legal children are
    already in (W).

If the starting position is not in (W), White cannot force a win under
the model used by this pre-check.

**Input:**

``` text
reachable_graph.db
```

**Output:**

``` text
precheck_not_white_win.db
```

**Run:**

``` bash
python precheck_not_white_win.py
```

The project reports that this pre-check completes in approximately ten
minutes, compared with roughly 2.5 hours for the full retrograde
computation.

### 13.2 Black drawing-strategy DAG

**Script:**

``` text
build_black_strategy_tree.py
```

This script constructs a playable AND-OR strategy DAG intended to
demonstrate how Black can preserve a draw against every White choice.

-   At White-to-move nodes (AND nodes), all relevant safe children are
    expanded. This represents the need to account for every legal White
    choice.
-   At Black-to-move nodes (OR nodes), one safe child is selected. This
    represents Black choosing a drawing response.
-   Expansion stops at the defined hard terminals or repetition-cycle
    conditions.

**Input:**

``` text
reachable_graph.db
```

**Output:**

``` text
black_strategy_tree.db
```

The reported structure contains approximately 5.9 million nodes.

### 13.3 Strategy-DAG validation

**Script:**

``` text
validate_black_strategy_tree.py
```

The validator performs 17 checks. The most important reported checks
include:

-   There are no White-win leaves in the strategy structure.
-   Every White node has all required legal children expanded.
-   Black nodes provide a selected safe continuation.
-   The generated DAG conforms to the expected strategy structure.

**Inputs:**

``` text
black_strategy_tree.db
reachable_graph.db
```

**Run:**

``` bash
python validate_black_strategy_tree.py
```

The project reports a **17/17 PASS** result.

These checks are valuable independent evidence, but their strength
depends on whether the validator independently reconstructs and verifies
the relevant properties rather than merely trusting metadata written by
the builder.

------------------------------------------------------------------------

## 14. Reproducing the experiment

Run the following commands from inside the relevant variant directory.
Complete each phase successfully before starting the next one.

### Core pipeline

``` bash
# 1. Enumerate all legal boards
python board_generator_new_variant.py --workers 4

# 2. Generate legal move edges
python edge_generator_new_variant.py --workers 4 --chunks 8

# 3. Extract the graph reachable from the starting position
python build_reachable_graph_new_variant.py

# 4. Detect cycles and classify strongly connected components
python detect_cycles_scc.py

# 5. Check whether the 50-move rule can apply
python apply_50_move_rule.py

# 6. Run layer-by-layer retrograde analysis
python run_all_retrograde.py

# 7. Validate minimax consistency
python validate_minimax.py --sample 1000000
```

For Variant B, use the six-million-node validation sample if reproducing
the reported Variant B check:

``` bash
python validate_minimax.py --sample 6000000
```

### Additional Variant B checks

``` bash
# Independent White-winning-set pre-check
python precheck_not_white_win.py

# Build the Black drawing-strategy DAG
python build_black_strategy_tree.py

# Validate the strategy DAG
python validate_black_strategy_tree.py
```

### Reproducibility notes

-   Run commands from the correct variant directory.
-   Keep the generated SQLite databases between phases.
-   Ensure that a phase has completed before running a dependent phase.
-   Preserve logs, runtime measurements, database sizes, and validation
    output for the research record.
-   Record the Python, SQLite, NumPy, and SciPy versions used.
-   For a rigorous reproduction, verify that the starting-position
    encoding and all rule assumptions match the version of the source
    code being executed.

------------------------------------------------------------------------

## 15. Research conclusion

This project applies exhaustive state enumeration, directed move-graph
construction, reachability analysis, SCC-based cycle analysis, and
layer-by-layer retrograde computation to two 4×4 mini-chess variants.

The resulting reachable graphs contain tens of millions of positions and
hundreds of millions of legal move edges. The solver classifies every
reachable position according to the implemented terminal, capture,
insufficient-material, and draw-rule assumptions.

The reported initial-position outcome is:

-   **Variant A (NB2K): DRAW**
-   **Variant B (NBBK): DRAW**

Therefore, the research finds that **White does not have a forced win
from the starting position in either variant when both players play
optimally**.

The most important contribution of the core pipeline is the end-to-end
computational method: generate the legal state space, construct the move
graph, isolate the reachable game, analyse its cyclic structure, and
propagate game-theoretic values backwards until the starting position is
resolved.

The conclusion is conditional on the correctness of the encoded chess
rules, state representation, move generator, and draw handling. Those
assumptions should be documented and independently checked when
presenting the result as a formal computational proof.

------------------------------------------------------------------------

## 16. Citation and licence

If you use the code or results in your own work, cite:

> Mashimbyi, V. (2026). *Solving Two 4×4 Mini-Chess Variants: Exhaustive
> Enumeration, Retrograde Analysis, Cycle Detection, and Independent
> Draw-Strategy Certificates*. COS700 Research Report, University of
> Pretoria.

Add the repository's chosen licence information here if the source code
is distributed under an explicit licence.

------------------------------------------------------------------------

## Scope note

This document focuses on the computational research pipeline from
legal-board generation through graph construction, reachability, cycle
analysis, rule handling, retrograde analysis, and validation.
**Visualiser implementation, visual interface features, and
visualisation-specific instructions are intentionally excluded.**
