# Algorithm candidate screening sources and locked slices v1

SPDX-License-Identifier: MIT

## Status

This document freezes the static input to the candidate screen. It does not report an
Oracle measurement, qualify an implementation, or rank a candidate. The companion
`candidate-evidence.v1.json` deliberately has empty official and holdout references;
the measured `candidate-oracle-capture.v1` and `candidate-screening.v1` records must be
created only from terminal, hash-bound runs.

Static eligibility means only that one reviewable transformation and a conservative
proof path have been identified. Qualification still requires at least one permitted
structure with complete and correct small, medium, and large observations and an
equal-weight dynamic-instruction geometric-mean upper bound of at least 1.10. Oracle
speedup is a source-level upper bound, not the expected speedup of the compiler pass.

## Repository sources

- `docs/optimization/data/manifests/oracle-cleanroom.manifest.json` contains exactly
  198 cases: 99 baseline/optimized pairs, arranged as 11 families by three structures
  by three sizes. Every family has 18 cases and no orphan sidecar.
- `docs/optimization/data/oracle/cleanroom-full.plan.json` binds those 99 pairs to one
  FULL profile for the future baseline and optimized legs.
- `benchmarks/RESEARCH_MAPPING.md` keeps the earlier research estimates separate from
  measurement and identifies existing compiler capabilities that must not be renamed
  as new candidates.
- Java `PassRegistry.standard()` at the screening-base commit is the pass-identity
  source of truth for the immutable pre-implementation
  `docs/optimization/data/pass-registry.v2.json` export. Screening requires this base
  export to contain zero `candidate` lifecycle descriptors and uses it for automated
  overlap-ID checks. Candidate implementation must create a separately named
  executable export; it must not overwrite this base. The implementation review also inspected the
  current SCEV, affine summarization, induction, dependence, alias/ModRef, loop unroll,
  reduction, and ranked-recurrence implementations under `src/main/java/accela/pass/`.

## Locked family decisions

| Oracle family | Three Oracle structures | Locked implementation slice or terminal disposition | Existing pass overlap | Cost / risk |
|---|---|---|---|---|
| `bitset` | `boolean_and`, `boolean_or`, `boolean_xor` | **Blocked.** Scalar/vector representation packing has no locked end-to-end IR and target-capability contract in the RV64GC pipeline. No implementation candidate is created. | None owns the representation change. | high / high |
| `boom_ilp` | `reduction_multi_acc`, `dot_unroll4`, `independent_chains` | `candidate.integer-reduction-expansion`: integer scalar reduction expansion with a four-lane partial-unroll shape and a scalar remainder. Floating-point reassociation is forbidden. `independent_chains` is excluded from this slice and belongs to same-domain fusion. | `ir.reduction-pushdown`, `ir.loop-unroll` | medium / medium |
| `closed_form` | `linear_sum`, `quadratic_sum`, `triangular_recurrence` | `candidate.extended-affine-summarization`: extend the existing SCEV/Affine path to additional ordered affine/polynomial integer recurrences. Do not build a parallel recurrence framework. | `ir.indvar-domain-simplify`, `ir.affine-loop-summarization`, `ir.strength-reduction` | medium / medium |
| `dp_storage` | `two_row`, `three_row`, `reverse_single_row` | `candidate.dp-storage-contraction`: contract a bounded affine DP history to a proven ring/frontier layout while preserving dependence order and bounds. | `ir.sroa`, `ir.dead-store-elimination`, `ir.loop-load-elimination` | high / high |
| `finite_state` | `affine_mod97`, `branch_mod53`, `quadratic_mod31` | `candidate.finite-state-acceleration`: accelerate a pure deterministic transition over a proven small closed constant domain with jump-table or binary-lifting state composition. | `ir.affine-loop-summarization`, `ir.loop-unroll` | high / high |
| `fusion` | `single_temporary`, `stencil_producer`, `two_temporaries` | `candidate.same-domain-loop-fusion`: fuse adjacent canonical loops with identical domains, including temporary contraction only when dependence, alias, side-effect, and live-out proofs all succeed. Its qualification mapping also includes the existing `boom_ilp/independent_chains` Oracle structure, whose source transformation is four adjacent same-domain loops fused into one. | `ir.dead-store-elimination`, `ir.loop-interchange`, `ir.loop-unroll-and-jam`, `ir.loop-load-elimination` | high / high |
| `linear_transition` | `affine_2d`, `affine_scalar`, `fibonacci_2d` | `candidate.integer-linear-transition`: exponentiate only small fixed-dimensional integer affine state transitions under exact i32 modulo semantics. Floating-point reassociation is forbidden. | `ir.ranked-recurrence-tabulation`, `ir.affine-loop-summarization` | high / high |
| `memoization` | `binomial`, `fibonacci`, `grid_paths` | `candidate.rrt2-on-demand-memoization`: extend Ranked Recurrence Tabulation only for composite well-founded ranks, reachable-domain discovery, and bounded on-demand memoization. Ordinary one-dimensional Fibonacci tabulation is explicitly excluded. | `ir.ranked-recurrence-tabulation` | high / high |
| `prefix_scan` | `forward_prefix`, `reverse_suffix`, `weighted_prefix` | `candidate.prefix-scan-reuse`: replace repeated overlapping prefix/suffix aggregation with an order-equivalent incremental recurrence after alias, effect, empty-domain, bounds, and live-out proofs. | `ir.reduction-pushdown`, `ir.loop-strength-reduce`, `ir.loop-load-elimination` | high / high |
| `recursion_worklist` | `dfs_stack`, `divide_conquer_sum`, `tail_to_loop` | **Rejected as a whole family.** Tail recursion, divide-and-conquer flattening, and explicit DFS worklists do not share one matcher, transform, or legality proof. Do not split this family into implementation tickets in this screen. | `ir.ranked-recurrence-tabulation`, `ir.tail-recursion-elimination` | unknown / high |
| `structured_kernel` | `gemm_loop_order`, `stencil_boundary_split`, `transpose_reduction` | **Rejected as a whole family.** Loop-order selection, boundary splitting, and transpose/temporary fusion do not form one reviewable transform with a unified proof. Do not split this family into implementation tickets in this screen. | `ir.loop-interchange`, `ir.loop-unroll-and-jam`, `ir.loop-unroll`, `ir.loop-load-elimination` | unknown / high |

The eligible implementation order is not a qualification or performance order. Once
Oracle capture exists, qualified candidates are ordered by best permitted structure
geometric mean, then lower risk, lower implementation cost, and stable implementation
ID. The eight eligible IDs above are the complete implementation pool; this screen must
not add a ninth candidate or relax a terminal disposition.

## Proof contract

The obligation IDs in `candidate-evidence.v1.json` are the minimum fail-closed contract
for each slice. An implementation must emit an observable candidate decision and reject
when any obligation is unproven. All eligible slices preserve exact signed 32-bit
two's-complement wrap semantics, input/output behavior, the RV64GC LP64D ABI, and the
runtime contract. They may inspect semantic IR and target properties only; benchmark,
path, function, source, input, and hash identities are forbidden matcher inputs.

## Structure-level screening boundary

`candidate-screening-spec.v1` binds every candidate to an ordered
`eligible_oracle_structure_refs` set. Each reference carries both `oracle_family_id` and
`structure_id`; this keeps the candidate capability taxonomy separate from the immutable
physical pair identity. The schema and semantic validator require an eligible family to
name at least one captured reference, require blocked or rejected families to name none,
reject unknown, duplicate, or reordered references, and prevent excluded structures from
qualifying a candidate.

The single locked cross-family mapping is `boom_ilp/independent_chains` to
`candidate.same-domain-loop-fusion`. The pair remains in its original `boom_ilp` family
inside the generator, manifest, plan, and capture, so the fixed 99 pairs and eleven
physical Oracle families do not change; the screening result retains that source family
while allowing the structure to qualify `fusion`. It cannot qualify integer reduction
expansion. Likewise, `memoization/fibonacci` cannot qualify RRT2 and is not remapped to
another candidate. The measured screening record preserves permitted references and all
captured structures, so excluded Oracle results remain visible without affecting
qualification.
