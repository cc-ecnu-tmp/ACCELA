package accela.pass;

import static accela.pass.PassDescriptor.Stage.BACKEND_FUNCTION;
import static accela.pass.PassDescriptor.Stage.BACKEND_MODULE;
import static accela.pass.PassDescriptor.Stage.IR_FUNCTION;
import static accela.pass.PassDescriptor.Stage.IR_MODULE;

import accela.util.StrictJson;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;

/** Single source of truth for every transformation scheduled by the full compiler pipeline. */
public final class PassRegistry {
  public static final String FAMILY_IR_CFG_SIMPLIFICATION = "ir.cfg-simplification";
  public static final String FAMILY_IR_AGGREGATE_SCALARIZATION = "ir.aggregate-scalarization";
  public static final String FAMILY_IR_SSA_CONSTRUCTION = "ir.ssa-construction";
  public static final String FAMILY_IR_CSE = "ir.common-subexpression-elimination";
  public static final String FAMILY_IR_CONSTANT_PROPAGATION = "ir.constant-propagation";
  public static final String FAMILY_IR_INSTRUCTION_COMBINING = "ir.instruction-combining";
  public static final String FAMILY_IR_DEAD_CODE_ELIMINATION = "ir.dead-code-elimination";
  public static final String FAMILY_IR_GLOBAL_OPTIMIZATION = "ir.global-optimization";
  public static final String FAMILY_IR_RANKED_RECURRENCE_TABULATION =
      "ir.ranked-recurrence-tabulation";
  public static final String FAMILY_IR_REDUCTION_PUSHDOWN = "ir.reduction-pushdown";
  public static final String FAMILY_IR_INDUCTION_VARIABLE_OPTIMIZATION =
      "ir.induction-variable-optimization";
  public static final String FAMILY_IR_AFFINE_LOOP_SUMMARIZATION =
      "ir.affine-loop-summarization";
  public static final String FAMILY_IR_LOOP_INTERCHANGE = "ir.loop-interchange";
  public static final String FAMILY_IR_LOOP_ROTATION = "ir.loop-rotation";
  public static final String FAMILY_IR_LOOP_UNROLLING = "ir.loop-unrolling";
  public static final String FAMILY_INTEGER_CONSTANT_ARITHMETIC_STRENGTH_REDUCTION =
      "integer.constant-arithmetic-strength-reduction";
  public static final String FAMILY_IR_LOOP_MEMORY_OPTIMIZATION =
      "ir.loop-memory-optimization";
  public static final String FAMILY_IR_INLINING = "ir.inlining";
  public static final String FAMILY_IR_TAIL_RECURSION = "ir.tail-recursion-elimination";

  public static final String FAMILY_BACKEND_MACHINE_IR_CONSTRUCTION =
      "backend.machine-ir-construction";
  public static final String FAMILY_BACKEND_COPY_PROPAGATION = "backend.copy-propagation";
  public static final String FAMILY_BACKEND_SSA_DESTRUCTION = "backend.ssa-destruction";
  public static final String FAMILY_BACKEND_ADDRESS_OPTIMIZATION =
      "backend.address-optimization";
  public static final String FAMILY_BACKEND_CSE = "backend.common-subexpression-elimination";
  public static final String FAMILY_BACKEND_LOOP_OPTIMIZATION = "backend.loop-optimization";
  public static final String FAMILY_BACKEND_BLOCK_LAYOUT = "backend.block-layout";
  public static final String FAMILY_BACKEND_REGISTER_ALLOCATION =
      "backend.register-allocation";
  public static final String FAMILY_BACKEND_EMISSION = "backend.emission";

  public static final String IR_SIMPLIFY_CFG = "ir.simplify-cfg";
  public static final String IR_SROA = "ir.sroa";
  public static final String IR_MEM2REG = "ir.mem2reg";
  public static final String IR_EARLY_CSE = "ir.early-cse";
  public static final String IR_SCCP = "ir.sccp";
  public static final String IR_INST_SIMPLIFY = "ir.inst-simplify";
  public static final String IR_INST_COMBINE = "ir.inst-combine";
  public static final String IR_ADCE = "ir.adce";
  public static final String IR_DEAD_STORE_ELIMINATION = "ir.dead-store-elimination";
  public static final String IR_GLOBAL_DCE = "ir.global-dce";
  public static final String IR_GLOBAL_OPT = "ir.global-opt";
  public static final String IR_GLOBAL_SROA = "ir.global-sroa";
  public static final String IR_IPSCCP = "ir.ipsccp";
  public static final String IR_RANKED_RECURRENCE_TABULATION = "ir.ranked-recurrence-tabulation";
  public static final String IR_INDVAR_DOMAIN_SIMPLIFY = "ir.indvar-domain-simplify";
  public static final String IR_AFFINE_LOOP_SUMMARIZATION = "ir.affine-loop-summarization";
  public static final String IR_REDUCTION_PUSHDOWN = "ir.reduction-pushdown";
  public static final String IR_LOOP_INTERCHANGE = "ir.loop-interchange";
  public static final String IR_LOOP_ROTATE = "ir.loop-rotate";
  public static final String IR_LICM = "ir.licm";
  public static final String IR_LOOP_UNROLL_AND_JAM = "ir.loop-unroll-and-jam";
  public static final String IR_LOOP_UNROLL = "ir.loop-unroll";
  public static final String IR_INDVAR_SIMPLIFY = "ir.indvar-simplify";
  public static final String IR_GVN = "ir.gvn";
  public static final String IR_LOOP_STRENGTH_REDUCE = "ir.loop-strength-reduce";
  public static final String IR_LOOP_LOAD_ROTATION = "ir.loop-load-rotation";
  public static final String IR_LOOP_LOAD_ELIMINATION = "ir.loop-load-elimination";
  public static final String IR_POINTER_LFTR = "ir.pointer-lftr";
  public static final String IR_STRENGTH_REDUCTION = "ir.strength-reduction";
  public static final String IR_TAIL_RECURSION_ELIMINATION = "ir.tail-recursion-elimination";
  public static final String IR_INLINER = "ir.inliner";

  public static final String BACKEND_IR_LOWERING = "backend.ir-to-machine-lowering";
  public static final String BACKEND_COPY_PROPAGATION = "backend.copy-propagation";
  public static final String BACKEND_PHI_ELIMINATION = "backend.phi-elimination";
  public static final String BACKEND_MEMORY_ADDRESS_FOLDING = "backend.memory-address-folding";
  public static final String BACKEND_MACHINE_CSE = "backend.machine-cse";
  public static final String BACKEND_GLOBAL_MERGE = "backend.global-merge";
  public static final String BACKEND_MACHINE_LICM = "backend.machine-licm";
  public static final String BACKEND_LOOP_CONDITION_DUPLICATION =
      "backend.loop-condition-duplication";
  public static final String BACKEND_CONSTANT_CSE = "backend.constant-cse";
  public static final String BACKEND_GLOBAL_ADDRESS_MATERIALIZATION =
      "backend.global-address-materialization";
  public static final String BACKEND_BLOCK_PLACEMENT = "backend.block-placement";
  public static final String BACKEND_REGISTER_ALLOCATION = "backend.register-allocation";
  public static final String BACKEND_BRANCH_FOLDING = "backend.branch-folding";
  public static final String BACKEND_ASM_EMISSION = "backend.asm-emission";
  public static final String BACKEND_RISCV_STRENGTH_REDUCTION =
      "backend.riscv-strength-reduction";

  public static final String EXPORT_SCHEMA_VERSION = "pass-registry.v2";

  private static final PassRegistry STANDARD = buildStandard();

  private final Map<String, PassDescriptor> descriptors;

  private PassRegistry(Collection<PassDescriptor> descriptors) {
    LinkedHashMap<String, PassDescriptor> byId = new LinkedHashMap<>();
    for (PassDescriptor descriptor : descriptors) {
      PassDescriptor previous = byId.putIfAbsent(descriptor.id(), descriptor);
      if (previous != null) throw new IllegalArgumentException("duplicate pass id: " + descriptor.id());
    }
    for (PassDescriptor descriptor : byId.values()) {
      if (!descriptor.candidate()) continue;
      PassDescriptor.CandidateAnchor anchor = descriptor.candidateAnchor();
      PassDescriptor anchored = byId.get(anchor.passId());
      if (anchored == null) {
        throw new IllegalArgumentException(
            "candidate '" + descriptor.id() + "' anchors to unknown pass '"
                + anchor.passId() + "'");
      }
      if (anchored.candidate()) {
        throw new IllegalArgumentException(
            "candidate '" + descriptor.id() + "' cannot anchor to another candidate");
      }
      if (anchored.stage() != descriptor.stage()) {
        throw new IllegalArgumentException(
            "candidate '" + descriptor.id() + "' anchor has a different pipeline stage");
      }
      if (anchor.occurrence() > anchored.fullPipelineOccurrences()) {
        throw new IllegalArgumentException(
            "candidate '" + descriptor.id() + "' anchor occurrence is outside pass '"
                + anchored.id() + "'");
      }
    }
    this.descriptors = Collections.unmodifiableMap(byId);
  }

  public static PassRegistry standard() {
    return STANDARD;
  }

  /** Builds an immutable validated registry, primarily for candidate integration and tests. */
  public static PassRegistry of(Collection<PassDescriptor> descriptors) {
    Objects.requireNonNull(descriptors, "descriptors");
    return new PassRegistry(descriptors);
  }

  public PassDescriptor require(String id) {
    PassDescriptor descriptor = descriptors.get(id);
    if (descriptor == null) throw new IllegalArgumentException("unknown pass '" + id + "'");
    return descriptor;
  }

  public PassDescriptor find(String id) {
    return descriptors.get(id);
  }

  public List<PassDescriptor> all() {
    return List.copyOf(descriptors.values());
  }

  public List<PassDescriptor> candidates() {
    return descriptors.values().stream().filter(PassDescriptor::candidate).toList();
  }

  public List<PassDescriptor> forStage(PassDescriptor.Stage stage) {
    return descriptors.values().stream().filter(pass -> pass.stage() == stage).toList();
  }

  public List<String> families() {
    return descriptors.values().stream().map(PassDescriptor::logicalFamilyId).distinct().toList();
  }

  public List<PassDescriptor> forFamily(String familyId) {
    List<PassDescriptor> family = descriptors.values().stream()
        .filter(pass -> pass.logicalFamilyId().equals(familyId)).toList();
    if (family.isEmpty()) throw new IllegalArgumentException("unknown pass family '" + familyId + "'");
    return family;
  }

  /** Returns the deterministic machine-readable registry consumed by benchmark tooling. */
  public String toJson() {
    LinkedHashMap<String, Object> root = new LinkedHashMap<>();
    root.put("schema_version", EXPORT_SCHEMA_VERSION);
    List<Map<String, Object>> passes = new ArrayList<>();
    for (PassDescriptor descriptor : descriptors.values()) {
      LinkedHashMap<String, Object> pass = new LinkedHashMap<>();
      pass.put("id", descriptor.id());
      pass.put("logical_family_id", descriptor.logicalFamilyId());
      pass.put("display_name", descriptor.displayName());
      pass.put("stage", descriptor.stage().name().toLowerCase(Locale.ROOT));
      pass.put("full_pipeline_occurrences", descriptor.fullPipelineOccurrences());
      pass.put("lifecycle", descriptor.lifecycle().name().toLowerCase(Locale.ROOT));
      pass.put("decision_observable", descriptor.decisionObservable());
      PassDescriptor.CandidateAnchor anchor = descriptor.candidateAnchor();
      if (anchor == null) {
        pass.put("candidate_anchor", null);
      } else {
        LinkedHashMap<String, Object> serializedAnchor = new LinkedHashMap<>();
        serializedAnchor.put("pass", anchor.passId());
        serializedAnchor.put("occurrence", anchor.occurrence());
        serializedAnchor.put("position", anchor.position().name().toLowerCase(Locale.ROOT));
        pass.put("candidate_anchor", Collections.unmodifiableMap(serializedAnchor));
      }
      pass.put("legality_obligation_ids", descriptor.legalityObligationIds());
      passes.add(Collections.unmodifiableMap(pass));
    }
    root.put("passes", List.copyOf(passes));
    return StrictJson.stringify(root);
  }

  /** Writes a UTF-8 registry snapshot without embedding any machine-local metadata. */
  public void writeJson(Path path) throws IOException {
    Objects.requireNonNull(path, "path");
    Files.writeString(path, toJson() + "\n", StandardCharsets.UTF_8);
  }

  private static PassRegistry buildStandard() {
    List<PassDescriptor> passes = new ArrayList<>();
    passes.add(pass(IR_SIMPLIFY_CFG, FAMILY_IR_CFG_SIMPLIFICATION, "Simplify CFG", IR_FUNCTION, 5));
    passes.add(pass(IR_SROA, FAMILY_IR_AGGREGATE_SCALARIZATION, "Scalar replacement of aggregates", IR_FUNCTION, 2));
    passes.add(required(IR_MEM2REG, FAMILY_IR_SSA_CONSTRUCTION, "Promote memory to SSA", IR_FUNCTION, 1));
    passes.add(pass(IR_EARLY_CSE, FAMILY_IR_CSE, "Early common subexpression elimination", IR_FUNCTION, 7));
    passes.add(pass(IR_SCCP, FAMILY_IR_CONSTANT_PROPAGATION, "Sparse conditional constant propagation", IR_FUNCTION, 3));
    passes.add(pass(IR_INST_SIMPLIFY, FAMILY_IR_INSTRUCTION_COMBINING, "Instruction simplification", IR_FUNCTION, 4));
    passes.add(pass(IR_INST_COMBINE, FAMILY_IR_INSTRUCTION_COMBINING, "Instruction combining", IR_FUNCTION, 2));
    passes.add(pass(IR_ADCE, FAMILY_IR_DEAD_CODE_ELIMINATION, "Aggressive dead-code elimination", IR_FUNCTION, 3));
    passes.add(pass(IR_DEAD_STORE_ELIMINATION, FAMILY_IR_DEAD_CODE_ELIMINATION, "Dead-store elimination", IR_MODULE, 1));
    passes.add(pass(IR_GLOBAL_DCE, FAMILY_IR_DEAD_CODE_ELIMINATION, "Global dead-code elimination", IR_MODULE, 3));
    passes.add(pass(IR_GLOBAL_OPT, FAMILY_IR_GLOBAL_OPTIMIZATION, "Global scalar optimization", IR_MODULE, 2));
    passes.add(pass(IR_GLOBAL_SROA, FAMILY_IR_AGGREGATE_SCALARIZATION, "Global aggregate scalarization", IR_MODULE, 1));
    passes.add(pass(IR_IPSCCP, FAMILY_IR_CONSTANT_PROPAGATION, "Interprocedural SCCP", IR_MODULE, 2));
    passes.add(observablePass(IR_RANKED_RECURRENCE_TABULATION,
        FAMILY_IR_RANKED_RECURRENCE_TABULATION, "Ranked recurrence tabulation", IR_MODULE, 1));
    passes.add(pass(IR_INDVAR_DOMAIN_SIMPLIFY, FAMILY_IR_INDUCTION_VARIABLE_OPTIMIZATION, "Induction-domain simplification", IR_FUNCTION, 1));
    passes.add(observablePass(IR_AFFINE_LOOP_SUMMARIZATION,
        FAMILY_IR_AFFINE_LOOP_SUMMARIZATION, "Affine loop summarization", IR_FUNCTION, 1));
    passes.add(pass(IR_REDUCTION_PUSHDOWN, FAMILY_IR_REDUCTION_PUSHDOWN,
        "Reduction pushdown", IR_FUNCTION, 1));
    passes.add(pass(IR_LOOP_INTERCHANGE, FAMILY_IR_LOOP_INTERCHANGE,
        "Loop interchange", IR_FUNCTION, 1));
    passes.add(pass(IR_LOOP_ROTATE, FAMILY_IR_LOOP_ROTATION,
        "Loop rotation", IR_FUNCTION, 1));
    passes.add(pass(IR_LICM, FAMILY_IR_LOOP_MEMORY_OPTIMIZATION, "Loop-invariant code motion", IR_FUNCTION, 2));
    passes.add(pass(IR_LOOP_UNROLL_AND_JAM, FAMILY_IR_LOOP_UNROLLING,
        "Loop unroll-and-jam", IR_FUNCTION, 1));
    passes.add(pass(IR_LOOP_UNROLL, FAMILY_IR_LOOP_UNROLLING,
        "Loop unrolling", IR_FUNCTION, 2));
    passes.add(pass(IR_INDVAR_SIMPLIFY, FAMILY_IR_INDUCTION_VARIABLE_OPTIMIZATION, "Induction-variable simplification", IR_FUNCTION, 1));
    passes.add(pass(IR_GVN, FAMILY_IR_CSE, "Global value numbering", IR_FUNCTION, 1));
    passes.add(pass(IR_LOOP_STRENGTH_REDUCE, FAMILY_IR_INDUCTION_VARIABLE_OPTIMIZATION, "Loop strength reduction", IR_FUNCTION, 1));
    passes.add(pass(IR_LOOP_LOAD_ROTATION, FAMILY_IR_LOOP_MEMORY_OPTIMIZATION, "Rotated-loop load elimination", IR_FUNCTION, 1));
    passes.add(pass(IR_LOOP_LOAD_ELIMINATION, FAMILY_IR_LOOP_MEMORY_OPTIMIZATION, "Loop load elimination", IR_FUNCTION, 1));
    passes.add(pass(IR_POINTER_LFTR, FAMILY_IR_INDUCTION_VARIABLE_OPTIMIZATION, "Pointer loop-test replacement", IR_FUNCTION, 1));
    passes.add(pass(IR_STRENGTH_REDUCTION,
        FAMILY_INTEGER_CONSTANT_ARITHMETIC_STRENGTH_REDUCTION,
        "Constant signed division and remainder strength reduction", IR_FUNCTION, 1));
    passes.add(pass(IR_TAIL_RECURSION_ELIMINATION, FAMILY_IR_TAIL_RECURSION, "Tail-recursion elimination", IR_FUNCTION, 1));
    passes.add(pass(IR_INLINER, FAMILY_IR_INLINING, "Function inlining", IR_MODULE, 1));

    passes.add(required(BACKEND_IR_LOWERING, FAMILY_BACKEND_MACHINE_IR_CONSTRUCTION, "IR to machine lowering", BACKEND_MODULE, 1));
    passes.add(pass(BACKEND_COPY_PROPAGATION, FAMILY_BACKEND_COPY_PROPAGATION, "Machine copy propagation", BACKEND_FUNCTION, 1));
    passes.add(required(BACKEND_PHI_ELIMINATION, FAMILY_BACKEND_SSA_DESTRUCTION, "Machine PHI elimination", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_MEMORY_ADDRESS_FOLDING, FAMILY_BACKEND_ADDRESS_OPTIMIZATION, "Memory-address folding", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_MACHINE_CSE, FAMILY_BACKEND_CSE, "Machine common subexpression elimination", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_GLOBAL_MERGE, FAMILY_BACKEND_ADDRESS_OPTIMIZATION, "Global-address merging", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_MACHINE_LICM, FAMILY_BACKEND_LOOP_OPTIMIZATION, "Machine loop-invariant code motion", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_LOOP_CONDITION_DUPLICATION, FAMILY_BACKEND_LOOP_OPTIMIZATION, "Loop-condition duplication", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_CONSTANT_CSE, FAMILY_BACKEND_CSE, "Machine constant CSE", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_GLOBAL_ADDRESS_MATERIALIZATION, FAMILY_BACKEND_ADDRESS_OPTIMIZATION, "Global-address materialization", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_BLOCK_PLACEMENT, FAMILY_BACKEND_BLOCK_LAYOUT, "Machine block placement", BACKEND_FUNCTION, 2));
    passes.add(required(BACKEND_REGISTER_ALLOCATION, FAMILY_BACKEND_REGISTER_ALLOCATION, "Register allocation", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_BRANCH_FOLDING, FAMILY_BACKEND_BLOCK_LAYOUT, "Post-allocation branch folding", BACKEND_FUNCTION, 1));
    passes.add(pass(BACKEND_RISCV_STRENGTH_REDUCTION,
        FAMILY_INTEGER_CONSTANT_ARITHMETIC_STRENGTH_REDUCTION,
        "RISC-V constant arithmetic strength reduction", BACKEND_MODULE, 1));
    passes.add(required(BACKEND_ASM_EMISSION, FAMILY_BACKEND_EMISSION, "RISC-V frame lowering and assembly emission", BACKEND_MODULE, 1));
    return new PassRegistry(passes);
  }

  private static PassDescriptor pass(
      String id, String family, String displayName, PassDescriptor.Stage stage, int occurrences) {
    return new PassDescriptor(
        id, family, displayName, stage, occurrences,
        PassDescriptor.Lifecycle.PRODUCTION, false, null, List.of());
  }

  private static PassDescriptor observablePass(
      String id, String family, String displayName, PassDescriptor.Stage stage, int occurrences) {
    return new PassDescriptor(
        id, family, displayName, stage, occurrences,
        PassDescriptor.Lifecycle.PRODUCTION, true, null, List.of());
  }

  private static PassDescriptor required(
      String id, String family, String displayName, PassDescriptor.Stage stage, int occurrences) {
    return new PassDescriptor(
        id, family, displayName, stage, occurrences,
        PassDescriptor.Lifecycle.REQUIRED, false, null, List.of());
  }
}
