package accela.cost;

import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.MachineCSE;
import accela.backend.lowering.MemoryAddressFolding;
import accela.backend.lowering.PhiElimination;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineVerifier;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.IteratedRegisterAllocator;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import java.util.ArrayDeque;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Shared module-level lowering, target-cost and DryRunRA evaluation for R2 Beam states. */
public final class ModuleCostEvaluator {
  public record Evaluation(
      CostEstimate cost,
      AllocationEstimate allocation,
      boolean recursiveCallGraph,
      Map<String, Double> unboundedLoopInstructionSlopes) {}

  private final SchedulerPolicy policy;
  private final RISCVTarget target = new RISCVTarget();
  private final RegisterAllocator allocator = new IteratedRegisterAllocator();
  private final MachineCostModel costModel;

  public ModuleCostEvaluator(TargetProfile profile) {
    if (profile == null) throw new IllegalArgumentException("target profile is required");
    policy = profile.scheduler();
    costModel = new MachineCostModel(profile, allocator, target);
  }

  public Evaluation evaluate(accela.ir.Module module) {
    MachineModule machine = new IRToMachineLowering(target).lower(module);
    for (MachineFunction function : machine.getFunctions()) {
      new PhiElimination().run(function);
      new MemoryAddressFolding().run(function);
      new MachineCSE().run(function);
      MachineVerifier.verify(function);
    }
    InvocationWeights invocations = invocationWeights(machine);
    CostEstimate total = zeroCost();
    AllocationEstimate allocation = zeroAllocation();
    Map<String, Double> slopes = new LinkedHashMap<>();
    for (MachineFunction function : machine.getFunctions()) {
      double weight = invocations.weights().getOrDefault(function.getName(), 0.0);
      CostEstimate functionCost = costModel.estimate(function);
      total = add(total, scaleRuntimeCost(functionCost, weight));
      if (weight != 0.0) {
        for (Map.Entry<String, Double> loop :
            costModel.unboundedLoopInstructionSlopes(function).entrySet()) {
          slopes.merge(loop.getKey(), checkedProduct(weight, loop.getValue()), Double::sum);
        }
      }
      allocation = add(allocation, allocator.estimate(function, target));
    }
    return new Evaluation(total, allocation, invocations.recursive(), Map.copyOf(slopes));
  }

  public boolean safelyDominates(Evaluation baseline, Evaluation candidate) {
    if (candidate.recursiveCallGraph()) return false;
    if (!noWorseUnboundedLoopSlopes(
        baseline.unboundedLoopInstructionSlopes(), candidate.unboundedLoopInstructionSlopes())) {
      return false;
    }
    CostEstimate base = baseline.cost();
    CostEstimate next = candidate.cost();
    if (!(next.robustScore(policy) < base.robustScore(policy))) return false;
    return noWorse(next.criticalPath(), base.criticalPath())
        && noWorse(next.frontend(), base.frontend())
        && noWorse(next.resources(), base.resources())
        && noWorse(next.memory(), base.memory())
        && noWorse(next.branch(), base.branch())
        && noWorse(next.spill(), base.spill())
        && noWorse(next.codeSize(), base.codeSize());
  }

  private InvocationWeights invocationWeights(MachineModule module) {
    Map<String, MachineFunction> functions = new LinkedHashMap<>();
    for (MachineFunction function : module.getFunctions()) functions.put(function.getName(), function);
    Map<String, Map<String, Double>> calls = new LinkedHashMap<>();
    for (String name : functions.keySet()) calls.put(name, new LinkedHashMap<>());
    for (MachineFunction function : functions.values()) {
      Map<MachineBasicBlock, Double> blockWeights = costModel.executionWeights(function);
      for (MachineBasicBlock block : function.getBlocks()) {
        double blockWeight = blockWeights.getOrDefault(block, 1.0);
        for (MachineInstr instruction : block.getInstructions()) {
          if (instruction.getOpcode() == MachineOpcode.CALL
              && functions.containsKey(instruction.getCallee())) {
            calls.get(function.getName()).merge(
                instruction.getCallee(), blockWeight, Double::sum);
          }
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
      for (String function : reachable) weights.putIfAbsent(function, 1.0);
    }
    return new InvocationWeights(Map.copyOf(weights), recursive);
  }

  private static Set<String> callGraphRoots(
      Set<String> functions, Map<String, Map<String, Double>> calls) {
    Set<String> roots = new LinkedHashSet<>(functions);
    for (Map<String, Double> outgoing : calls.values()) roots.removeAll(outgoing.keySet());
    return roots.isEmpty() ? Set.copyOf(functions) : Set.copyOf(roots);
  }

  private static void collectReachable(
      String root, Map<String, Map<String, Double>> calls, Set<String> reachable) {
    ArrayDeque<String> pending = new ArrayDeque<>();
    pending.add(root);
    while (!pending.isEmpty()) {
      String current = pending.removeLast();
      if (!reachable.add(current)) continue;
      for (String callee : calls.get(current).keySet()) pending.addLast(callee);
    }
  }

  private static boolean noWorseUnboundedLoopSlopes(
      Map<String, Double> baseline, Map<String, Double> candidate) {
    List<Double> base = baseline.values().stream().sorted(Comparator.reverseOrder()).toList();
    List<Double> next = candidate.values().stream().sorted(Comparator.reverseOrder()).toList();
    int count = Math.max(base.size(), next.size());
    for (int index = 0; index < count; index++) {
      double baseSlope = index < base.size() ? base.get(index) : 0.0;
      double nextSlope = index < next.size() ? next.get(index) : 0.0;
      if (!noWorse(nextSlope, baseSlope)) return false;
    }
    return true;
  }

  private static boolean noWorse(double candidate, double baseline) {
    double tolerance = Math.ulp(Math.max(Math.abs(candidate), Math.abs(baseline))) * 4.0;
    return candidate <= baseline + tolerance;
  }

  private static double checkedProduct(double left, double right) {
    double value = left * right;
    if (!Double.isFinite(value)) throw new IllegalStateException("R2 call weight overflow");
    return value;
  }

  static CostEstimate scaleRuntimeCost(CostEstimate cost, double weight) {
    return new CostEstimate(cost.cycles() * weight, cost.uncertainty() * Math.sqrt(weight),
        cost.criticalPath() * weight, cost.frontend() * weight, cost.resources() * weight,
        cost.memory() * weight, cost.branch() * weight, cost.spill() * weight, cost.codeSize());
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

  private static CostEstimate zeroCost() {
    return new CostEstimate(0, 0, 0, 0, 0, 0, 0, 0, 0);
  }

  private static AllocationEstimate zeroAllocation() {
    return new AllocationEstimate(0, 0, 0, 0, 0, 0);
  }

  private record InvocationWeights(Map<String, Double> weights, boolean recursive) {}
}
