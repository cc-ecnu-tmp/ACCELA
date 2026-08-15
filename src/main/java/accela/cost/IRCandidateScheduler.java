package accela.cost;

import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.MachineCSE;
import accela.backend.lowering.MemoryAddressFolding;
import accela.backend.lowering.PhiElimination;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineVerifier;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.IteratedRegisterAllocator;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import accela.ir.IRSnapshot;
import accela.pass.PassBuilder;
import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.PipelineProfile;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.transform.LICM;
import accela.pass.ir.transform.EarlyCSE;
import accela.pass.ir.transform.InstSimplify;
import accela.pass.ir.transform.SCCP;
import accela.pass.ir.transform.StrengthReduction;
import accela.pass.ir.transform.inliner.Inliner;
import accela.pass.ir.transform.loop.unroll.LoopUnroll;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;
import accela.pass.ir.verify.IRVerifier;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Transactional, bounded R1 beam over the selected IR profitability passes. */
public final class IRCandidateScheduler {
  static final String DEFERRED_R1_SCHEDULE_ID = "ir.schedule.deferred-r1";
  private final TargetProfile profile;
  private final DecisionTraceSink trace;
  private final RISCVTarget target = new RISCVTarget();
  private final RegisterAllocator allocator = new IteratedRegisterAllocator();
  private final MachineCostModel costModel;
  private int moduleExpansions;
  private final Map<Integer, Integer> functionExpansions = new HashMap<>();

  public IRCandidateScheduler(TargetProfile profile, DecisionTraceSink trace) {
    this.profile = java.util.Objects.requireNonNull(profile, "profile");
    this.trace = java.util.Objects.requireNonNull(trace, "trace");
    costModel = new MachineCostModel(profile, allocator, target);
  }

  public accela.ir.Module schedule(accela.ir.Module productionFull) {
    return schedule(productionFull, productionFull);
  }

  public accela.ir.Module schedule(
      accela.ir.Module productionFull, accela.ir.Module candidateStaging) {
    IRVerifier.verifyModule(productionFull);
    IRVerifier.verifyModule(candidateStaging);
    State initial = state(IRSnapshot.deepCopy(productionFull), List.of());
    State staging = state(IRSnapshot.deepCopy(candidateStaging),
        List.of(DEFERRED_R1_SCHEDULE_ID));
    if (!profile.calibrated() || !profile.scheduler().enabled()) {
      trace.accept(new DecisionTraceSink.Decision(profile.id(),
          profile.evidenceLevel().name().toLowerCase(), "ir.beam.final", "module", "module",
          "rejected", !profile.calibrated() ? "profile_not_calibrated" : "scheduler_disabled",
          "not_evaluated", "ir.beam.validated-state", Map.of("sequence", ""), initial.cost(),
          initial.cost(), initial.allocation(), 0, profile.scheduler().maxModuleExpansions()));
      return initial.module();
    }

    List<Spec> specs = new ArrayList<>();
    PipelineProfile pipeline = PipelineProfile.r1();
    PassDescriptor licm = pipeline.require(PassRegistry.IR_LICM);
    PassDescriptor unroll1 = pipeline.require(PassRegistry.IR_UNROLL_1);
    PassDescriptor unroll2 = pipeline.require(PassRegistry.IR_UNROLL_2);
    PassDescriptor strength = pipeline.require(PassRegistry.IR_STRENGTH);
    PassDescriptor inliner = pipeline.require(PassRegistry.IR_INLINER);
    for (int index = 0; index < candidateStaging.getFunctions().size(); index++) {
      String function = candidateStaging.getFunctions().get(index).getName();
      specs.add(functionSpec(licm.id(), licm.primaryObligation(), index, function,
          (candidate, functionIndex, fam) -> LICM.runOnFunction(
              candidate.getFunctions().get(functionIndex), fam)));
      specs.add(functionSpec(unroll1.id(), unroll1.primaryObligation(), index, function,
          (candidate, functionIndex, fam) -> LoopUnroll.run(
              candidate.getFunctions().get(functionIndex), fam)));
      specs.add(functionSpec(unroll2.id(), unroll2.primaryObligation(), index, function,
          (candidate, functionIndex, fam) -> LoopUnroll.run(
              candidate.getFunctions().get(functionIndex), fam)));
      specs.add(functionSpec(strength.id(), strength.primaryObligation(), index,
          function, (candidate, functionIndex, fam) -> StrengthReduction.runOnFunction(
              candidate.getFunctions().get(functionIndex))));
    }
    specs.add(new Spec(inliner.id(), inliner.primaryObligation(), -1, "module",
        candidate -> {
          PassBuilder builder = new PassBuilder();
          FunctionAnalysisManager fam = builder.buildFunctionAnalysisManager();
          boolean changed = new Inliner.Pass().run(candidate, builder.buildModuleAnalysisManager(),
              fam).preservesNone();
          if (changed) {
            for (accela.ir.Function function : candidate.getFunctions()) {
              new SimplifyCFG.Pass().run(function, fam);
              fam.invalidate(function, accela.pass.PreservedAnalyses.none());
              new EarlyCSE.Pass().run(function, fam);
              fam.invalidate(function, accela.pass.PreservedAnalyses.none());
              new SCCP.Pass().run(function, fam);
              fam.invalidate(function, accela.pass.PreservedAnalyses.none());
              new InstSimplify.Pass().run(function, fam);
              fam.invalidate(function, accela.pass.PreservedAnalyses.none());
            }
          }
          return changed;
        }));

    List<State> beam = List.of(staging);
    for (Spec spec : specs) {
      if (moduleExpansions >= profile.scheduler().maxModuleExpansions()) {
        emitBudget(spec, "module", moduleExpansions, profile.scheduler().maxModuleExpansions());
        break;
      }
      List<State> expanded = new ArrayList<>(beam);
      for (State baseline : beam) {
        if (!hasBudget(spec)) continue;
        moduleExpansions++;
        if (spec.functionIndex() >= 0) functionExpansions.merge(spec.functionIndex(), 1, Integer::sum);
        accela.ir.Module candidate = IRSnapshot.deepCopy(baseline.module());
        boolean changed = spec.transform().apply(candidate);
        if (!changed) {
          trace.accept(decision(spec, "rejected", "no_change", baseline.cost(), null, null));
          continue;
        }
        IRVerifier.verifyModule(candidate);
        State transformed = state(candidate, append(baseline.sequence(), spec.id()));
        trace.accept(decision(spec, "considered", "proved_and_costed", baseline.cost(),
            transformed.cost(), transformed.allocation()));
        expanded.add(transformed);
      }
      expanded.sort(Comparator.<State>comparingDouble(
              state -> state.cost().robustScore(profile.scheduler()))
          .thenComparing(state -> String.join("\u0000", state.sequence())));
      beam = List.copyOf(expanded.subList(0,
          Math.min(profile.scheduler().beamWidth(), expanded.size())));
    }
    int recursivePruned = 0;
    int unknownLoopPruned = 0;
    int costPruned = 0;
    List<State> eligible = new ArrayList<>();
    for (State state : beam) {
      if (state.sequence().isEmpty()) continue;
      if (state.recursiveCallGraph()) {
        recursivePruned++;
      } else if (!noWorseUnboundedLoopSlopes(
          initial.unboundedLoopInstructionSlopes(), state.unboundedLoopInstructionSlopes())) {
        unknownLoopPruned++;
      } else if (!safelyDominates(initial.cost(), state.cost())) {
        costPruned++;
      } else {
        eligible.add(state);
      }
    }
    State selected = eligible.stream()
        .min(Comparator.<State>comparingDouble(
                state -> state.cost().robustScore(profile.scheduler()))
            .thenComparing(state -> String.join("\u0000", state.sequence())))
        .orElse(initial);
    Map<String, String> finalParameters = new LinkedHashMap<>();
    finalParameters.put("sequence", String.join(",", selected.sequence()));
    finalParameters.put("recursive_call_graph_pruned", Integer.toString(recursivePruned));
    finalParameters.put("unbounded_loop_slope_pruned", Integer.toString(unknownLoopPruned));
    finalParameters.put("cost_vector_pruned", Integer.toString(costPruned));
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), "ir.beam.final", "module", "module",
        selected.sequence().isEmpty() ? "rejected" : "applied",
        selected.sequence().isEmpty() ? "baseline_selected" : "best_validated_state",
        selected.sequence().isEmpty() ? "not_applicable" : "proved",
        "ir.beam.validated-state", finalParameters,
        initial.cost(), selected.cost(),
        selected.allocation(), moduleExpansions, profile.scheduler().maxModuleExpansions()));
    return selected.module();
  }

  private boolean safelyDominates(CostEstimate baseline, CostEstimate candidate) {
    if (!(candidate.robustScore(profile.scheduler())
        < baseline.robustScore(profile.scheduler()))) return false;
    return noWorse(candidate.criticalPath(), baseline.criticalPath())
        && noWorse(candidate.frontend(), baseline.frontend())
        && noWorse(candidate.resources(), baseline.resources())
        && noWorse(candidate.memory(), baseline.memory())
        && noWorse(candidate.branch(), baseline.branch())
        && noWorse(candidate.spill(), baseline.spill())
        && noWorse(candidate.codeSize(), baseline.codeSize());
  }

  private static boolean noWorse(double candidate, double baseline) {
    double tolerance = Math.ulp(Math.max(Math.abs(candidate), Math.abs(baseline))) * 4.0;
    return candidate <= baseline + tolerance;
  }

  private boolean hasBudget(Spec spec) {
    if (moduleExpansions >= profile.scheduler().maxModuleExpansions()) return false;
    if (spec.functionIndex() < 0) return true;
    int used = functionExpansions.getOrDefault(spec.functionIndex(), 0);
    if (used < profile.scheduler().maxFunctionExpansions()) return true;
    emitBudget(spec, "function", used, profile.scheduler().maxFunctionExpansions());
    return false;
  }

  private void emitBudget(Spec spec, String scope, int used, int budget) {
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), spec.id(), spec.targetKind(),
        spec.targetName(), "rejected", "budget_exhausted", "not_evaluated", spec.obligation(),
        Map.of("scope", scope), null, null,
        null, used, budget));
  }

  private DecisionTraceSink.Decision decision(Spec spec, String status, String reason,
      CostEstimate baseline, CostEstimate transformed, AllocationEstimate allocation) {
    return new DecisionTraceSink.Decision(profile.id(), profile.evidenceLevel().name().toLowerCase(),
        spec.id(), spec.targetKind(), spec.targetName(), status, reason,
        "no_change".equals(reason) ? "not_applicable" : "proved", spec.obligation(), Map.of(),
        baseline, transformed,
        allocation, moduleExpansions, profile.scheduler().maxModuleExpansions());
  }

  private State state(accela.ir.Module module, List<String> sequence) {
    MachineModule machine = new IRToMachineLowering(target).lower(module);
    for (MachineFunction function : machine.getFunctions()) {
      new PhiElimination().run(function);
      new MemoryAddressFolding().run(function);
      new MachineCSE().run(function);
      MachineVerifier.verify(function);
    }
    InvocationWeights invocationWeights = invocationWeights(machine);
    CostEstimate total = new CostEstimate(0, 0, 0, 0, 0, 0, 0, 0, 0);
    AllocationEstimate allocation = new AllocationEstimate(0, 0, 0, 0, 0, 0);
    Map<String, Double> unboundedLoopInstructionSlopes = new LinkedHashMap<>();
    for (MachineFunction function : machine.getFunctions()) {
      double weight = invocationWeights.weights().getOrDefault(function.getName(), 0.0);
      if (weight == 0.0) continue;
      total = add(total, scaleRuntimeCost(costModel.estimate(function), weight));
      for (Map.Entry<String, Double> loop :
          costModel.unboundedLoopInstructionSlopes(function).entrySet()) {
        unboundedLoopInstructionSlopes.merge(
            loop.getKey(), checkedProduct(weight, loop.getValue()), Double::sum);
      }
      allocation = add(allocation, allocator.estimate(function, target));
    }
    return new State(module, total, allocation, List.copyOf(sequence),
        invocationWeights.recursive(), Map.copyOf(unboundedLoopInstructionSlopes));
  }

  static boolean noWorseUnboundedLoopSlopes(
      Map<String, Double> baseline, Map<String, Double> candidate) {
    List<Double> baselineSlopes = baseline.values().stream()
        .sorted(Comparator.reverseOrder()).toList();
    List<Double> candidateSlopes = candidate.values().stream()
        .sorted(Comparator.reverseOrder()).toList();
    int count = Math.max(baselineSlopes.size(), candidateSlopes.size());
    for (int index = 0; index < count; index++) {
      double baselineSlope = index < baselineSlopes.size() ? baselineSlopes.get(index) : 0.0;
      double candidateSlope = index < candidateSlopes.size() ? candidateSlopes.get(index) : 0.0;
      if (!noWorse(candidateSlope, baselineSlope)) return false;
    }
    return true;
  }

  private InvocationWeights invocationWeights(MachineModule module) {
    Map<String, MachineFunction> functions = new LinkedHashMap<>();
    for (MachineFunction function : module.getFunctions()) functions.put(function.getName(), function);
    Map<String, Map<String, Double>> calls = new LinkedHashMap<>();
    for (String name : functions.keySet()) {
      calls.put(name, new LinkedHashMap<>());
    }
    for (MachineFunction function : functions.values()) {
      Map<MachineBasicBlock, Double> blockWeights = costModel.executionWeights(function);
      for (MachineBasicBlock block : function.getBlocks()) {
        double blockWeight = blockWeights.getOrDefault(block, 1.0);
        for (MachineInstr instruction : block.getInstructions()) {
          if (instruction.getOpcode() != MachineOpcode.CALL
              || !functions.containsKey(instruction.getCallee())) continue;
          calls.get(function.getName()).merge(instruction.getCallee(), blockWeight, Double::sum);
        }
      }
    }
    Set<String> roots = functions.containsKey("main")
        ? Set.of("main") : callGraphRoots(functions.keySet(), calls);
    Set<String> reachable = new LinkedHashSet<>();
    for (String root : roots) collectReachable(root, calls, reachable);
    Map<String, Integer> indegree = new LinkedHashMap<>();
    for (String function : reachable) indegree.put(function, 0);
    for (String caller : reachable) {
      for (String callee : calls.get(caller).keySet()) {
        if (reachable.contains(callee)) indegree.merge(callee, 1, Integer::sum);
      }
    }
    ArrayDeque<String> ready = new ArrayDeque<>();
    for (Map.Entry<String, Integer> entry : indegree.entrySet()) {
      if (entry.getValue() == 0) ready.addLast(entry.getKey());
    }
    Map<String, Double> weights = new LinkedHashMap<>();
    for (String root : roots) weights.put(root, 1.0);
    int visited = 0;
    while (!ready.isEmpty()) {
      String caller = ready.removeFirst();
      visited++;
      double callerWeight = weights.getOrDefault(caller, 0.0);
      for (Map.Entry<String, Double> call : calls.get(caller).entrySet()) {
        weights.merge(call.getKey(), checkedProduct(callerWeight, call.getValue()), Double::sum);
        if (indegree.merge(call.getKey(), -1, Integer::sum) == 0) ready.addLast(call.getKey());
      }
    }
    boolean recursive = visited != reachable.size();
    if (recursive) {
      // Cyclic call counts are unbounded without runtime depth evidence. Keep costs finite for
      // diagnostics, but mark the state ineligible for alternate schedule selection.
      for (String function : reachable) weights.putIfAbsent(function, 1.0);
    }
    return new InvocationWeights(Map.copyOf(weights), recursive);
  }

  private static Set<String> callGraphRoots(
      Set<String> functions, Map<String, Map<String, Double>> calls) {
    Set<String> roots = new LinkedHashSet<>(functions);
    for (Map<String, Double> outgoing : calls.values()) roots.removeAll(outgoing.keySet());
    // A module consisting only of mutually recursive functions has no structural root. Mark every
    // function reachable so that the cycle is observed and the alternate schedule fails closed.
    return roots.isEmpty() ? Set.copyOf(functions) : Set.copyOf(roots);
  }

  private static void collectReachable(
      String function, Map<String, Map<String, Double>> calls, Set<String> reachable) {
    ArrayDeque<String> pending = new ArrayDeque<>();
    pending.addLast(function);
    while (!pending.isEmpty()) {
      String current = pending.removeLast();
      if (!reachable.add(current)) continue;
      for (String callee : calls.get(current).keySet()) pending.addLast(callee);
    }
  }

  private static double checkedProduct(double left, double right) {
    double product = left * right;
    if (!Double.isFinite(product)) {
      throw new IllegalStateException("module call-frequency weight overflow");
    }
    return product;
  }

  private static CostEstimate scaleRuntimeCost(CostEstimate cost, double weight) {
    return new CostEstimate(cost.cycles() * weight, cost.uncertainty() * Math.sqrt(weight),
        cost.criticalPath() * weight, cost.frontend() * weight, cost.resources() * weight,
        cost.memory() * weight, cost.branch() * weight, cost.spill() * weight,
        cost.codeSize());
  }

  private static CostEstimate add(CostEstimate left, CostEstimate right) {
    return new CostEstimate(left.cycles() + right.cycles(),
        Math.hypot(left.uncertainty(), right.uncertainty()),
        left.criticalPath() + right.criticalPath(), left.frontend() + right.frontend(),
        left.resources() + right.resources(), left.memory() + right.memory(),
        left.branch() + right.branch(), left.spill() + right.spill(),
        left.codeSize() + right.codeSize());
  }

  private static AllocationEstimate add(AllocationEstimate left, AllocationEstimate right) {
    return new AllocationEstimate(left.predictedSpills() + right.predictedSpills(),
        left.spillWeight() + right.spillWeight(),
        Math.max(left.maxIntegerLive(), right.maxIntegerLive()),
        Math.max(left.maxFloatLive(), right.maxFloatLive()),
        left.calleeSaveCost() + right.calleeSaveCost(),
        left.coalescingLoss() + right.coalescingLoss());
  }

  private static List<String> append(List<String> values, String value) {
    List<String> result = new ArrayList<>(values);
    result.add(value);
    return result;
  }

  private static Spec functionSpec(String id, String obligation, int functionIndex,
      String functionName, FunctionTransform transform) {
    return new Spec(id, obligation, functionIndex, functionName, candidate -> {
      PassBuilder builder = new PassBuilder();
      return transform.apply(candidate, functionIndex, builder.buildFunctionAnalysisManager());
    });
  }

  @FunctionalInterface
  private interface Transform { boolean apply(accela.ir.Module candidate); }

  @FunctionalInterface
  private interface FunctionTransform {
    boolean apply(accela.ir.Module candidate, int functionIndex, FunctionAnalysisManager fam);
  }

  private record Spec(String id, String obligation, int functionIndex, String targetName,
      Transform transform) {
    String targetKind() { return functionIndex < 0 ? "module" : "ir-function"; }
  }

  private record InvocationWeights(Map<String, Double> weights, boolean recursive) {}

  private record State(accela.ir.Module module, CostEstimate cost, AllocationEstimate allocation,
      List<String> sequence, boolean recursiveCallGraph,
      Map<String, Double> unboundedLoopInstructionSlopes) {}
}
