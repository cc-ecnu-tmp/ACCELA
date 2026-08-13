# Research-to-corpus mapping

SPDX-License-Identifier: MIT

The earlier optimization discussion ranked its first tier as minimal-RV SIMD,
arbitrary-constant division/remainder, RRT 2.0, and BOOM-aware
unroll/multi-accumulator/scheduling. Its second tier was 2D tiling,
unswitch/peeling, LSR/address optimization, and post-register-allocation work.
All associated speedup and geometric-mean ranges were engineering estimates,
not measurements on the final target.

The current benchmark plan changes that interpretation:

| Earlier item | Current status and evidence role |
|---|---|
| Minimal-RV SIMD | Blocked until the 2026 target ISA and rules are published. RV64GC does not itself promise a vector extension. The bitset oracle checks representation equivalence only. |
| Arbitrary constant div/rem | Implemented in the current compiler and therefore an ablation/baseline item, not a new candidate family. Boundary correctness and target instruction quality still require measurement. |
| Affine loop summarization | The restricted implementation is present. The closed-form oracle family measures coverage and missed/generalized forms without assuming that all report patterns are implemented. |
| RRT | The current RRT is present and must be isolated in ablation. RRT 2.0 denotes proposed recurrence-framework extensions, not proof that memo/rolling/worklist lowerings already exist. |
| BOOM-aware unroll/ILP scheduling | Represented directly by boom_ilp oracles for multi-accumulator reduction, four-way dot-product unrolling, and independent-chain scheduling. |
| 2D tiling and kernel scheduling | Covered by the primary dense/stencil programs and structured-kernel oracles. Performance still requires BOOM or final-board evidence. |
| Unswitch/peeling, LSR/address, post-RA | Retained as second-tier backend/loop evaluation topics; they are not substituted for the 11 algorithmic/ILP oracle families. |

The newer research report prioritizes proven semantic recovery and
algorithm-level transformations over isolated peepholes. The corpus therefore
keeps three evidence layers separate:

1. the 22 primary workload programs for end-to-end scoring;
2. 33 baseline/optimized oracle pairs for candidate-family semantics;
3. 60 clean-room structural variants for CFG/induction robustness without
   using official source or case fingerprints.

No row in this mapping is a performance result. A candidate moves in priority
only after correctness, actual match evidence, per-case timing/instruction
counts, coverage, and weighted geometric mean have been collected.
