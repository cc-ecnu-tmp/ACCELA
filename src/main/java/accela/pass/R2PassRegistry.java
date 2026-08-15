package accela.pass;

import static accela.pass.R2PassOccurrence.Analysis.CALL_GRAPH;
import static accela.pass.R2PassOccurrence.Analysis.DOMINATOR_TREE;
import static accela.pass.R2PassOccurrence.Analysis.INDUCTION_VARIABLES;
import static accela.pass.R2PassOccurrence.Analysis.INTERFERENCE;
import static accela.pass.R2PassOccurrence.Analysis.LIVENESS;
import static accela.pass.R2PassOccurrence.Analysis.LOOP_INFO;
import static accela.pass.R2PassOccurrence.Analysis.POST_DOMINATOR_TREE;
import static accela.pass.R2PassOccurrence.Analysis.SCALAR_EVOLUTION;
import static accela.pass.R2PassOccurrence.Scope.FUNCTION;
import static accela.pass.R2PassOccurrence.Scope.MODULE;

import accela.util.StrictJson;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Frozen occurrence-level DAG for every pass in the current production FULL pipeline. */
public final class R2PassRegistry {
  public static final String SCHEMA_VERSION = "pass-registry.r2.v1";
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
  public static final String IR_RRT = "ir.ranked-recurrence-tabulation";
  public static final String IR_INDVAR_DOMAIN = "ir.indvar-domain-simplify";
  public static final String IR_AFFINE_SUMMARY = "ir.affine-loop-summarization";
  public static final String IR_REDUCTION_PUSHDOWN = "ir.reduction-pushdown";
  public static final String IR_LOOP_INTERCHANGE = "ir.loop-interchange";
  public static final String IR_LOOP_ROTATE = "ir.loop-rotate";
  public static final String IR_LICM = "ir.licm";
  public static final String IR_UNROLL_AND_JAM = "ir.loop-unroll-and-jam";
  public static final String IR_UNROLL = "ir.loop-unroll";
  public static final String IR_INDVAR_SIMPLIFY = "ir.indvar-simplify";
  public static final String IR_GVN = "ir.gvn";
  public static final String IR_LOOP_STRENGTH = "ir.loop-strength-reduce";
  public static final String IR_LOOP_LOAD_ROTATE = "ir.loop-load-rotation";
  public static final String IR_LOOP_LOAD_ELIMINATION = "ir.loop-load-elimination";
  public static final String IR_POINTER_LFTR = "ir.pointer-lftr";
  public static final String IR_STRENGTH = "ir.strength-reduction";
  public static final String IR_TAIL_RECURSION = "ir.tail-recursion-elimination";
  public static final String IR_INLINER = "ir.inliner";

  public static final String LOWERING = "backend.ir-lowering";
  public static final String COPY_PROPAGATION = "backend.copy-propagation";
  public static final String PHI_ELIMINATION = "backend.phi-elimination";
  public static final String ADDRESS_FOLDING = "backend.memory-address-folding";
  public static final String MACHINE_CSE = "backend.machine-cse";
  public static final String GLOBAL_MERGE = "backend.global-merge";
  public static final String MACHINE_LICM = "backend.machine-licm";
  public static final String LOOP_CONDITION_DUPLICATION = "backend.loop-condition-duplication";
  public static final String CONSTANT_CSE = "backend.constant-cse";
  public static final String GLOBAL_ADDRESS = "backend.global-address-materialization";
  public static final String BLOCK_PLACEMENT = "backend.block-placement";
  public static final String REGISTER_ALLOCATION = "backend.register-allocation";
  public static final String BRANCH_FOLDING = "backend.branch-folding";
  public static final String EMISSION = "backend.asm-emission";

  private static final Set<R2PassOccurrence.Analysis> IR_ANALYSES =
      Collections.unmodifiableSet(EnumSet.of(DOMINATOR_TREE, POST_DOMINATOR_TREE,
          LOOP_INFO, INDUCTION_VARIABLES, SCALAR_EVOLUTION, CALL_GRAPH));
  private static final Set<R2PassOccurrence.Analysis> LOOP_ANALYSES =
      Collections.unmodifiableSet(EnumSet.of(DOMINATOR_TREE, LOOP_INFO,
          INDUCTION_VARIABLES, SCALAR_EVOLUTION));
  private static final Set<R2PassOccurrence.Analysis> MACHINE_ANALYSES =
      Collections.unmodifiableSet(EnumSet.of(LIVENESS, INTERFERENCE));

  private static final R2PassRegistry PRODUCTION = buildProduction();

  private final Map<String, R2PassOccurrence> occurrences;
  private final List<R2PassOccurrence> order;

  private R2PassRegistry(List<R2PassOccurrence> order) {
    LinkedHashMap<String, R2PassOccurrence> byId = new LinkedHashMap<>();
    int stage = -1;
    for (R2PassOccurrence occurrence : order) {
      if (byId.putIfAbsent(occurrence.id(), occurrence) != null) {
        throw new IllegalArgumentException("duplicate R2 occurrence: " + occurrence.id());
      }
      if (occurrence.stage().ordinal() < stage) {
        throw new IllegalArgumentException("R2 stage regresses at " + occurrence.id());
      }
      stage = occurrence.stage().ordinal();
      for (String dependency : occurrence.dependencies()) {
        if (!byId.containsKey(dependency)) {
          throw new IllegalArgumentException(
              "R2 dependency must precede occurrence: " + dependency + " -> " + occurrence.id());
        }
      }
    }
    this.occurrences = Collections.unmodifiableMap(byId);
    this.order = List.copyOf(order);
    requireExactlyOneRequired(IR_MEM2REG);
    requireExactlyOneRequired(LOWERING);
    requireExactlyOneRequired(PHI_ELIMINATION);
    requireExactlyOneRequired(REGISTER_ALLOCATION);
    requireExactlyOneRequired(EMISSION);
  }

  public static R2PassRegistry production() { return PRODUCTION; }

  public List<R2PassOccurrence> all() { return order; }

  public R2PassOccurrence require(String id) {
    R2PassOccurrence occurrence = occurrences.get(id);
    if (occurrence == null) throw new IllegalArgumentException("unknown R2 occurrence: " + id);
    return occurrence;
  }

  public List<R2PassOccurrence> family(String familyId) {
    List<R2PassOccurrence> result = order.stream()
        .filter(occurrence -> occurrence.familyId().equals(familyId)).toList();
    if (result.isEmpty()) throw new IllegalArgumentException("unknown R2 family: " + familyId);
    return result;
  }

  public List<String> fullDecisionOrder() {
    return order.stream().map(R2PassOccurrence::id).toList();
  }

  /** Returns a deterministic machine-readable snapshot for diagnostics and field reports. */
  public String toJson() {
    LinkedHashMap<String, Object> root = new LinkedHashMap<>();
    root.put("schema_version", SCHEMA_VERSION);
    List<Map<String, Object>> serialized = new ArrayList<>();
    for (R2PassOccurrence occurrence : order) {
      LinkedHashMap<String, Object> item = new LinkedHashMap<>();
      item.put("id", occurrence.id());
      item.put("family_id", occurrence.familyId());
      item.put("stage", occurrence.stage().name().toLowerCase(java.util.Locale.ROOT));
      item.put("scope", occurrence.scope().name().toLowerCase(java.util.Locale.ROOT));
      item.put("required", occurrence.required());
      item.put("dependencies", occurrence.dependencies());
      item.put("required_analyses", occurrence.requiredAnalyses().stream()
          .map(value -> value.name().toLowerCase(java.util.Locale.ROOT)).sorted().toList());
      item.put("invalidated_analyses", occurrence.invalidatedAnalyses().stream()
          .map(value -> value.name().toLowerCase(java.util.Locale.ROOT)).sorted().toList());
      serialized.add(Collections.unmodifiableMap(item));
    }
    root.put("occurrences", List.copyOf(serialized));
    return StrictJson.stringify(root);
  }

  public void writeJson(Path path) throws IOException {
    if (path == null) throw new IllegalArgumentException("R2 registry output path is required");
    Files.writeString(path, toJson() + "\n", StandardCharsets.UTF_8);
  }

  private void requireExactlyOneRequired(String familyId) {
    List<R2PassOccurrence> family = family(familyId);
    if (family.size() != 1 || !family.getFirst().required()) {
      throw new IllegalArgumentException("R2 required boundary is invalid: " + familyId);
    }
  }

  private static R2PassRegistry buildProduction() {
    Builder builder = new Builder();
    builder.irFunction(IR_SIMPLIFY_CFG, false);
    builder.irFunction(IR_SROA, false);
    builder.irFunction(IR_MEM2REG, true);
    builder.irFunction(IR_EARLY_CSE, false);
    builder.irFunction(IR_SCCP, false);
    builder.irFunctionUnordered(IR_EARLY_CSE, IR_INST_SIMPLIFY);
    builder.irFunction(IR_SROA, false);
    builder.irFunction(IR_SCCP, false);
    builder.irFunctionUnordered(IR_EARLY_CSE, IR_INST_SIMPLIFY);
    builder.irFunction(IR_INST_COMBINE, false);
    builder.irFunction(IR_ADCE, false);
    builder.irFunction(IR_SIMPLIFY_CFG, false);
    builder.irFunction(IR_EARLY_CSE, false);

    builder.irModule(IR_DEAD_STORE_ELIMINATION, false);
    builder.irModule(IR_GLOBAL_DCE, false);
    builder.irModule(IR_GLOBAL_OPT, false);
    builder.irModule(IR_GLOBAL_SROA, false);
    builder.irModule(IR_IPSCCP, false);
    builder.irModule(IR_RRT, false);
    builder.irFunction(IR_EARLY_CSE, false);
    builder.irFunction(IR_TAIL_RECURSION, false);
    builder.irModule(IR_INLINER, false);
    builder.irFunction(IR_SIMPLIFY_CFG, false);
    builder.irFunction(IR_EARLY_CSE, false);
    builder.irFunction(IR_SCCP, false);
    builder.irFunction(IR_INST_SIMPLIFY, false);
    builder.irFunction(IR_INST_COMBINE, false);
    builder.irFunction(IR_ADCE, false);
    builder.irFunction(IR_SIMPLIFY_CFG, false);
    builder.irModule(IR_IPSCCP, false);
    builder.irModule(IR_GLOBAL_DCE, false);
    builder.irModule(IR_GLOBAL_OPT, false);

    builder.irLoop(IR_INDVAR_DOMAIN);
    builder.irLoop(IR_AFFINE_SUMMARY);
    builder.irLoop(IR_REDUCTION_PUSHDOWN);
    builder.irLoop(IR_LOOP_INTERCHANGE);
    builder.irLoop(IR_LOOP_ROTATE);
    builder.irLoop(IR_LICM);
    builder.irFunction(IR_EARLY_CSE, false);
    builder.irLoop(IR_UNROLL_AND_JAM);
    builder.irLoop(IR_UNROLL);
    builder.irLoop(IR_UNROLL);
    builder.irLoop(IR_INDVAR_SIMPLIFY);
    builder.irFunction(IR_INST_SIMPLIFY, false);
    builder.irFunction(IR_SIMPLIFY_CFG, false);
    builder.irLoop(IR_GVN);
    builder.irLoop(IR_LOOP_STRENGTH);
    builder.irLoop(IR_LOOP_LOAD_ROTATE);
    builder.irLoop(IR_LOOP_LOAD_ELIMINATION);
    builder.irLoop(IR_POINTER_LFTR);
    builder.irLoop(IR_LICM);
    builder.irFunction(IR_STRENGTH, false);
    builder.irFunction(IR_ADCE, false);
    builder.irModule(IR_GLOBAL_DCE, false);

    builder.add(LOWERING, PassDescriptor.Stage.LOWERING, MODULE, true, Set.of(), IR_ANALYSES);
    builder.machine(COPY_PROPAGATION, false);
    builder.machine(PHI_ELIMINATION, true);
    builder.machineUnordered(ADDRESS_FOLDING, MACHINE_CSE);
    builder.machine(GLOBAL_MERGE, false);
    builder.machine(MACHINE_LICM, false);
    builder.machine(LOOP_CONDITION_DUPLICATION, false);
    builder.machine(CONSTANT_CSE, false);
    builder.machine(GLOBAL_ADDRESS, false);
    builder.machine(BLOCK_PLACEMENT, false);
    builder.add(REGISTER_ALLOCATION, PassDescriptor.Stage.REGISTER_ALLOCATION,
        FUNCTION, true, MACHINE_ANALYSES, Set.of());
    builder.add(BRANCH_FOLDING, PassDescriptor.Stage.REGISTER_ALLOCATION,
        FUNCTION, false, Set.of(), Set.of());
    builder.add(BLOCK_PLACEMENT, PassDescriptor.Stage.REGISTER_ALLOCATION,
        FUNCTION, false, Set.of(), Set.of());
    builder.add(EMISSION, PassDescriptor.Stage.EMISSION, MODULE, true, Set.of(), Set.of());
    return new R2PassRegistry(builder.occurrences);
  }

  private static final class Builder {
    private final List<R2PassOccurrence> occurrences = new ArrayList<>();
    private final Map<String, Integer> familyOccurrences = new LinkedHashMap<>();
    private List<String> frontier = List.of();

    void irFunction(String family, boolean required) {
      add(family, PassDescriptor.Stage.IR, FUNCTION, required, Set.of(), IR_ANALYSES);
    }

    /** Adds a verified cleanup window whose members may execute in either order. */
    void irFunctionUnordered(String first, String second) {
      List<String> dependencies = frontier;
      String firstId = addDependingOn(first, PassDescriptor.Stage.IR, FUNCTION, false,
          Set.of(), IR_ANALYSES, dependencies);
      String secondId = addDependingOn(second, PassDescriptor.Stage.IR, FUNCTION, false,
          Set.of(), IR_ANALYSES, dependencies);
      frontier = List.of(firstId, secondId);
    }

    void irModule(String family, boolean required) {
      add(family, PassDescriptor.Stage.IR, MODULE, required, Set.of(), IR_ANALYSES);
    }

    void irLoop(String family) {
      add(family, PassDescriptor.Stage.IR, FUNCTION, false, LOOP_ANALYSES, IR_ANALYSES);
    }

    void machine(String family, boolean required) {
      add(family, PassDescriptor.Stage.MIR, FUNCTION, required, Set.of(), MACHINE_ANALYSES);
    }

    void machineUnordered(String first, String second) {
      List<String> dependencies = frontier;
      String firstId = addDependingOn(first, PassDescriptor.Stage.MIR, FUNCTION, false,
          Set.of(), MACHINE_ANALYSES, dependencies);
      String secondId = addDependingOn(second, PassDescriptor.Stage.MIR, FUNCTION, false,
          Set.of(), MACHINE_ANALYSES, dependencies);
      frontier = List.of(firstId, secondId);
    }

    void add(String family, PassDescriptor.Stage stage, R2PassOccurrence.Scope scope,
        boolean required, Set<R2PassOccurrence.Analysis> analyses,
        Set<R2PassOccurrence.Analysis> invalidates) {
      String id = addDependingOn(family, stage, scope, required, analyses, invalidates, frontier);
      frontier = List.of(id);
    }

    String addDependingOn(String family, PassDescriptor.Stage stage,
        R2PassOccurrence.Scope scope, boolean required,
        Set<R2PassOccurrence.Analysis> analyses,
        Set<R2PassOccurrence.Analysis> invalidates, List<String> dependencies) {
      int occurrence = familyOccurrences.merge(family, 1, Integer::sum);
      String id = family + "." + occurrence;
      occurrences.add(new R2PassOccurrence(id, family, stage, scope, required,
          dependencies, analyses, invalidates));
      return id;
    }
  }
}
