package accela.cost;

import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.MachineCSE;
import accela.backend.lowering.MemoryAddressFolding;
import accela.backend.lowering.PhiElimination;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineVerifier;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.IteratedRegisterAllocator;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import accela.ir.IRSnapshot;
import accela.pass.PassBuilder;
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
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Transactional, bounded R1 beam over the selected IR profitability passes. */
public final class IRCandidateScheduler {
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

  public accela.ir.Module schedule(accela.ir.Module input) {
    IRVerifier.verifyModule(input);
    State initial = state(IRSnapshot.deepCopy(input), List.of());
    if (!profile.calibrated() || !profile.scheduler().enabled()) {
      trace.accept(new DecisionTraceSink.Decision(profile.id(),
          profile.evidenceLevel().name().toLowerCase(), "ir.beam.final", "module", "module",
          "rejected", !profile.calibrated() ? "profile_not_calibrated" : "scheduler_disabled",
          "not_evaluated", "ir.beam.validated-state", Map.of("sequence", ""), initial.cost(),
          initial.cost(), initial.allocation(), 0, profile.scheduler().maxModuleExpansions()));
      return initial.module();
    }

    List<Spec> specs = new ArrayList<>();
    for (int index = 0; index < input.getFunctions().size(); index++) {
      String function = input.getFunctions().get(index).getName();
      specs.add(functionSpec("ir.licm", "ir.licm.memory-and-dominance", index, function,
          (candidate, functionIndex, fam) -> LICM.runOnFunction(
              candidate.getFunctions().get(functionIndex), fam)));
      specs.add(functionSpec("ir.loop-unroll.1", "ir.loop-unroll.constant-trip", index, function,
          (candidate, functionIndex, fam) -> LoopUnroll.run(
              candidate.getFunctions().get(functionIndex), fam)));
      specs.add(functionSpec("ir.loop-unroll.2", "ir.loop-unroll.constant-trip", index, function,
          (candidate, functionIndex, fam) -> LoopUnroll.run(
              candidate.getFunctions().get(functionIndex), fam)));
      specs.add(functionSpec("ir.strength-reduction", "ir.strength-reduction.signed-arithmetic", index,
          function, (candidate, functionIndex, fam) -> StrengthReduction.runOnFunction(
              candidate.getFunctions().get(functionIndex))));
    }
    specs.add(new Spec("ir.inliner", "ir.inliner.direct-call-and-return", -1, "module",
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

    List<State> beam = List.of(initial);
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
    State selected = beam.getFirst();
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), "ir.beam.final", "module", "module",
        "applied", selected.sequence().isEmpty() ? "baseline_selected" : "best_validated_state",
        "not_applicable", "ir.beam.validated-state", Map.of("sequence", String.join(",", selected.sequence())),
        initial.cost(), selected.cost(),
        selected.allocation(), moduleExpansions, profile.scheduler().maxModuleExpansions()));
    return selected.module();
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
    CostEstimate total = new CostEstimate(0, 0, 0, 0, 0, 0, 0, 0, 0);
    AllocationEstimate allocation = new AllocationEstimate(0, 0, 0, 0, 0, 0);
    for (MachineFunction function : machine.getFunctions()) {
      new PhiElimination().run(function);
      new MemoryAddressFolding().run(function);
      new MachineCSE().run(function);
      MachineVerifier.verify(function);
      total = add(total, costModel.estimate(function));
      allocation = add(allocation, allocator.estimate(function, target));
    }
    return new State(module, total, allocation, List.copyOf(sequence));
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

  private record State(accela.ir.Module module, CostEstimate cost, AllocationEstimate allocation,
      List<String> sequence) {}
}
