package accela.cost;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.ExactTripCount;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** A deterministic two-wide, dependency- and resource-aware Machine IR cost model. */
public final class MachineCostModel {
  private final TargetProfile profile;
  private final RegisterAllocator allocator;
  private final RISCVTarget target;

  public MachineCostModel(TargetProfile profile, RegisterAllocator allocator, RISCVTarget target) {
    this.profile = profile;
    this.allocator = allocator;
    this.target = target;
  }

  public CostEstimate estimate(MachineFunction function) {
    Map<MachineBasicBlock, Double> executionWeights = executionWeights(function);
    double instructionCount = 0.0;
    int codeBytes = 0;
    double branches = 0;
    double loads = 0;
    double stores = 0;
    double loadUseEdges = 0;
    double criticalPath = 0.0;
    double resourceCycles = 0.0;
    double variance = 0.0;

    for (MachineBasicBlock block : function.getBlocks()) {
      double weight = executionWeights.getOrDefault(block, 1.0);
      criticalPath += criticalPath(block) * weight;
      resourceCycles += simulate(block) * weight;
      loadUseEdges += loadUseEdges(block) * weight;
      for (MachineInstr instruction : block.getInstructions()) {
        InstructionClass instructionClass = InstructionClass.of(instruction.getOpcode());
        TargetProfile.OperationCost operation = profile.operation(instructionClass);
        instructionCount += weight;
        codeBytes += operation.codeBytes();
        if (instructionClass == InstructionClass.BRANCH
            || instructionClass == InstructionClass.CALL_RETURN) branches += weight;
        if (instructionClass == InstructionClass.LOAD) loads += weight;
        if (instructionClass == InstructionClass.STORE) stores += weight;
        variance += weight * square(operation.latency().mad());
        variance += weight * square(operation.reciprocalThroughput().mad());
      }
    }

    double frontend = Math.max(Math.ceil(instructionCount / profile.fetchWidth()),
        curveEstimate(profile.diagnostics().frontend(), codeBytes));
    double loadUseExtra = Math.max(0.0, profile.diagnostics().loadUse().median()
        - profile.operation(InstructionClass.LOAD).latency().median());
    double memory =
        loads * profile.operation(InstructionClass.LOAD).reciprocalThroughput().median()
            + stores * profile.operation(InstructionClass.STORE).reciprocalThroughput().median()
            + loadUseEdges * loadUseExtra;
    double branch = branches * profile.predictableBranch().median();
    AllocationEstimate allocation = allocator.estimate(function, target);
    double spill = allocation.spillWeight()
        * (profile.spillLoad().median() + profile.spillStore().median())
        + allocation.calleeSaveCost()
            * (profile.spillLoad().median() + profile.spillStore().median());
    double codeSize = codeBytes / (profile.fetchWidth() * 16.0);
    double bottleneck = Math.max(Math.max(criticalPath, frontend), Math.max(resourceCycles, memory));
    double cycles = bottleneck + branch + spill + codeSize;
    variance += branches * square(profile.predictableBranch().mad());
    variance += loadUseEdges * square(profile.diagnostics().loadUse().mad());
    variance += allocation.predictedSpills()
        * (square(profile.spillLoad().mad()) + square(profile.spillStore().mad()));
    return new CostEstimate(cycles, Math.sqrt(variance), criticalPath, frontend,
        resourceCycles, memory, branch, spill, codeSize);
  }

  Map<MachineBasicBlock, Double> executionWeights(MachineFunction machineFunction) {
    return analyzeExecutionWeights(machineFunction).executionWeights();
  }

  Map<String, Double> unboundedLoopInstructionSlopes(MachineFunction machineFunction) {
    ExecutionWeightAnalysis analysis = analyzeExecutionWeights(machineFunction);
    Map<String, Double> result = new LinkedHashMap<>();
    for (Map.Entry<String, Map<MachineBasicBlock, Double>> loop :
        analysis.unboundedLoopWeights().entrySet()) {
      double instructions = 0.0;
      for (Map.Entry<MachineBasicBlock, Double> block : loop.getValue().entrySet()) {
        instructions += block.getKey().getInstructions().size() * block.getValue();
      }
      result.put(loop.getKey(), instructions);
    }
    return Map.copyOf(result);
  }

  private ExecutionWeightAnalysis analyzeExecutionWeights(MachineFunction machineFunction) {
    Function sourceFunction = machineFunction.getBlocks().stream()
        .map(MachineBasicBlock::getSourceFunction)
        .filter(java.util.Objects::nonNull)
        .findFirst()
        .orElse(null);
    if (sourceFunction == null) return new ExecutionWeightAnalysis(Map.of(), Map.of());
    FunctionAnalysisManager analyses = new FunctionAnalysisManager();
    analyses.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    analyses.registerPass(LoopAnalysis.class, new LoopAnalysis());
    analyses.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    Map<LoopAnalysis.Loop, Integer> exactTrips = new IdentityHashMap<>();
    for (InductionVariableAnalysis.Induction induction :
        analyses.getResult(InductionVariableAnalysis.class, sourceFunction).allInductions()) {
      ExactTripCount exact = ExactTripCount.find(induction);
      if (exact != null && hasOnlyCanonicalExit(induction.loop(), exact.exit())) {
        exactTrips.put(induction.loop(), exact.count());
      }
    }
    List<LoopAnalysis.Loop> loops =
        analyses.getResult(LoopAnalysis.class, sourceFunction).loops();
    Map<BasicBlock, Double> sourceWeights = new IdentityHashMap<>();
    for (BasicBlock block : sourceFunction.getBlocks()) {
      double weight = 1.0;
      for (LoopAnalysis.Loop loop : loops) {
        if (loop.contains(block) && exactTrips.containsKey(loop)) {
          weight = checkedMultiply(weight, exactTrips.get(loop));
        }
      }
      sourceWeights.put(block, weight);
    }
    Map<MachineBasicBlock, Double> result = new IdentityHashMap<>();
    for (MachineBasicBlock block : machineFunction.getBlocks()) {
      result.put(block, sourceWeights.getOrDefault(block.getSourceBlock(), 1.0));
    }
    Map<String, Map<MachineBasicBlock, Double>> unbounded = new LinkedHashMap<>();
    for (LoopAnalysis.Loop loop : loops) {
      if (exactTrips.containsKey(loop)) continue;
      String key = sourceFunction.getName() + "::" + loop.header().getName();
      Map<MachineBasicBlock, Double> blocks = new IdentityHashMap<>();
      for (MachineBasicBlock machineBlock : machineFunction.getBlocks()) {
        BasicBlock sourceBlock = machineBlock.getSourceBlock();
        if (sourceBlock == null || !loop.contains(sourceBlock)) continue;
        double exactEnclosingWeight = 1.0;
        for (LoopAnalysis.Loop enclosing : loops) {
          if (enclosing.contains(sourceBlock) && exactTrips.containsKey(enclosing)) {
            exactEnclosingWeight = checkedMultiply(
                exactEnclosingWeight, exactTrips.get(enclosing));
          }
        }
        blocks.put(machineBlock, exactEnclosingWeight);
      }
      unbounded.put(key, Map.copyOf(blocks));
    }
    return new ExecutionWeightAnalysis(Map.copyOf(result), Map.copyOf(unbounded));
  }

  private static boolean hasOnlyCanonicalExit(LoopAnalysis.Loop loop, BasicBlock exit) {
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor) && (block != loop.header() || successor != exit)) {
          return false;
        }
      }
    }
    return true;
  }

  private static double checkedMultiply(double left, int right) {
    double product = left * right;
    if (!Double.isFinite(product)) {
      throw new IllegalStateException("exact loop execution weight overflow");
    }
    return product;
  }

  private int loadUseEdges(MachineBasicBlock block) {
    Map<VirtualRegister, InstructionClass> definitions = new HashMap<>();
    int edges = 0;
    for (MachineInstr instruction : block.getInstructions()) {
      for (var operand : instruction.getOperands()) {
        if (operand instanceof VRegOperand register
            && definitions.get(register.getRegister()) == InstructionClass.LOAD) edges++;
      }
      if (instruction.getDest() != null) {
        definitions.put(instruction.getDest(), InstructionClass.of(instruction.getOpcode()));
      }
    }
    return edges;
  }

  private static double curveEstimate(java.util.NavigableMap<Integer, Measurement> curve, int point) {
    Map.Entry<Integer, Measurement> ceiling = curve.ceilingEntry(Math.max(1, point));
    if (ceiling != null) return ceiling.getValue().median();
    Map.Entry<Integer, Measurement> last = curve.lastEntry();
    return last.getValue().median() * point / last.getKey();
  }

  private double criticalPath(MachineBasicBlock block) {
    Map<VirtualRegister, Double> readyAt = new HashMap<>();
    double longest = 0.0;
    for (MachineInstr instruction : block.getInstructions()) {
      double start = 0.0;
      for (var operand : instruction.getOperands()) {
        if (operand instanceof VRegOperand register) {
          start = Math.max(start, readyAt.getOrDefault(register.getRegister(), 0.0));
        }
      }
      double end = start + profile.operation(InstructionClass.of(instruction.getOpcode())).latency().median();
      if (instruction.getDest() != null) readyAt.put(instruction.getDest(), end);
      longest = Math.max(longest, end);
    }
    return longest;
  }

  private double simulate(MachineBasicBlock block) {
    List<Node> nodes = new ArrayList<>();
    Map<VirtualRegister, Node> definitions = new HashMap<>();
    int index = 0;
    for (MachineInstr instruction : block.getInstructions()) {
      Node node = new Node(index++, instruction, InstructionClass.of(instruction.getOpcode()));
      nodes.add(node);
      for (var operand : instruction.getOperands()) {
        if (operand instanceof VRegOperand register) {
          Node dependency = definitions.get(register.getRegister());
          if (dependency != null) {
            node.remainingDependencies++;
            dependency.consumers.add(node);
          }
        }
      }
      if (instruction.getDest() != null) definitions.put(instruction.getDest(), node);
    }

    int issued = 0;
    int cycle = 0;
    Map<String, Integer> resourceAvailable = new LinkedHashMap<>();
    while (issued < nodes.size()) {
      int currentCycle = cycle;
      List<Node> ready = nodes.stream()
          .filter(node -> !node.issued && node.remainingDependencies == 0 && node.readyAt <= currentCycle)
          .filter(node -> resourceAvailable.getOrDefault(resource(node), 0) <= currentCycle)
          .toList();
      if (ready.isEmpty()) {
        cycle++;
        continue;
      }
      Node first = ready.getFirst();
      issue(first, cycle, resourceAvailable);
      issued++;
      if (profile.issueWidth() > 1) {
        for (int candidateIndex = 1; candidateIndex < ready.size(); candidateIndex++) {
          Node second = ready.get(candidateIndex);
          if (profile.pairing(first.instructionClass, second.instructionClass).median() <= 1.0) {
            issue(second, cycle, resourceAvailable);
            issued++;
            break;
          }
        }
      }
      cycle++;
    }
    return cycle;
  }

  private void issue(Node node, int cycle, Map<String, Integer> resourceAvailable) {
    node.issued = true;
    TargetProfile.OperationCost operation = profile.operation(node.instructionClass);
    int completion = cycle + Math.max(1, (int) Math.ceil(operation.latency().median()));
    resourceAvailable.put(resource(node), cycle + Math.max(1, (int) Math.ceil(operation.resourceOccupancy())));
    for (Node consumer : node.consumers) {
      consumer.remainingDependencies--;
      consumer.readyAt = Math.max(consumer.readyAt, completion);
    }
  }

  private String resource(Node node) {
    return profile.operation(node.instructionClass).resource();
  }

  private static double square(double value) { return value * value; }

  private record ExecutionWeightAnalysis(
      Map<MachineBasicBlock, Double> executionWeights,
      Map<String, Map<MachineBasicBlock, Double>> unboundedLoopWeights) {}

  private static final class Node {
    final int index;
    final MachineInstr instruction;
    final InstructionClass instructionClass;
    final List<Node> consumers = new ArrayList<>();
    int remainingDependencies;
    int readyAt;
    boolean issued;

    Node(int index, MachineInstr instruction, InstructionClass instructionClass) {
      this.index = index;
      this.instruction = instruction;
      this.instructionClass = instructionClass;
    }
  }
}
