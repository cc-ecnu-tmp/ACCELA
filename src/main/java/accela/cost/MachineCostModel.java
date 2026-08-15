package accela.cost;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.BlockOperand;
import accela.backend.machine.ImmOperand;
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
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.LinkedHashSet;
import java.util.Set;

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
    return estimate(function, allocator.estimate(function, target));
  }

  /** Estimates an allocated or pre-RA function using the supplied DryRunRA result. */
  public CostEstimate estimate(MachineFunction function, AllocationEstimate allocation) {
    if (function == null || allocation == null) {
      throw new IllegalArgumentException("machine function and allocation estimate are required");
    }
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

    for (int blockIndex = 0; blockIndex < function.getBlocks().size(); blockIndex++) {
      MachineBasicBlock block = function.getBlocks().get(blockIndex);
      MachineBasicBlock fallthrough = blockIndex + 1 < function.getBlocks().size()
          ? function.getBlocks().get(blockIndex + 1) : null;
      double weight = executionWeights.getOrDefault(block, 1.0);
      criticalPath += criticalPath(block) * weight;
      resourceCycles += simulate(block) * weight;
      loadUseEdges += loadUseEdges(block) * weight;
      for (MachineInstr instruction : block.getInstructions()) {
        InstructionClass instructionClass = InstructionClass.of(instruction.getOpcode());
        TargetProfile.OperationCost operation = profile.operation(instructionClass);
        int emitted = emittedInstructionCount(instruction, fallthrough);
        instructionCount += emitted * weight;
        codeBytes += operation.codeBytes() * emitted;
        if (instructionClass == InstructionClass.BRANCH
            || instructionClass == InstructionClass.CALL_RETURN) {
          branches += emittedBranchCount(instruction, fallthrough) * weight;
        }
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
    int component = 0;
    for (double instructions : machineCycleUpperBounds(machineFunction)) {
      result.put("machine-cfg::" + component++, instructions);
    }
    return Map.copyOf(result);
  }

  List<Double> machineCycleUpperBounds(MachineFunction function) {
    return cyclicComponents(function).stream()
        .map(component -> component.stream()
            .mapToDouble(block -> emittedBlockInstructions(function, block)).sum())
        .sorted(Comparator.reverseOrder()).toList();
  }

  List<Double> machineCycleLowerBounds(MachineFunction function) {
    return cyclicComponents(function).stream()
        .map(component -> shortestCycle(function, component))
        .sorted(Comparator.reverseOrder()).toList();
  }

  private static double shortestCycle(
      MachineFunction function, Set<MachineBasicBlock> component) {
    double best = Double.POSITIVE_INFINITY;
    for (MachineBasicBlock start : component) {
      double startWeight = emittedBlockInstructions(function, start);
      for (MachineBasicBlock successor : successors(start)) {
        if (!component.contains(successor)) continue;
        if (successor == start) {
          best = Math.min(best, startWeight);
          continue;
        }
        Map<MachineBasicBlock, Double> distances = new IdentityHashMap<>();
        java.util.PriorityQueue<WeightedBlock> pending = new java.util.PriorityQueue<>(
            Comparator.comparingDouble(WeightedBlock::cost));
        distances.put(successor, startWeight);
        pending.add(new WeightedBlock(successor, startWeight));
        while (!pending.isEmpty()) {
          WeightedBlock current = pending.remove();
          if (current.cost() != distances.get(current.block())) continue;
          double nextCost = current.cost()
              + emittedBlockInstructions(function, current.block());
          for (MachineBasicBlock next : successors(current.block())) {
            if (!component.contains(next)) continue;
            if (next == start) {
              best = Math.min(best, nextCost);
            } else if (nextCost < distances.getOrDefault(next, Double.POSITIVE_INFINITY)) {
              distances.put(next, nextCost);
              pending.add(new WeightedBlock(next, nextCost));
            }
          }
        }
      }
    }
    if (!Double.isFinite(best)) {
      throw new IllegalStateException("cyclic Machine CFG has no cycle");
    }
    return best;
  }

  private static double emittedBlockInstructions(
      MachineFunction function, MachineBasicBlock block) {
    int index = function.getBlocks().indexOf(block);
    if (index < 0) throw new IllegalStateException("foreign block in Machine CFG cycle");
    MachineBasicBlock fallthrough = index + 1 < function.getBlocks().size()
        ? function.getBlocks().get(index + 1) : null;
    return block.getInstructions().stream()
        .mapToInt(instruction -> emittedInstructionCount(instruction, fallthrough)).sum();
  }

  /** Counts target instructions emitted by one MIR instruction, including implicit materialization. */
  static int emittedInstructionCount(MachineInstr instruction, MachineBasicBlock fallthrough) {
    if (instruction.getOpcode() == accela.backend.machine.MachineOpcode.BR) {
      if (instruction.getOperands().size() != 1
          || !(instruction.getOperands().getFirst() instanceof BlockOperand target)) {
        throw new IllegalStateException("malformed unconditional Machine IR branch");
      }
      return target.getBlock() == fallthrough ? 0 : 1;
    }
    int count = 1;
    if (instruction.getOpcode() == accela.backend.machine.MachineOpcode.CONDBR) {
      if (instruction.getPredicate() == null) {
        if (!instruction.getOperands().isEmpty()
            && instruction.getOperands().getFirst() instanceof ImmOperand) count++;
      } else {
        if (!instruction.getOperands().isEmpty()
            && instruction.getOperands().getFirst() instanceof ImmOperand) count++;
        if (instruction.getOperands().size() > 1
            && instruction.getOperands().get(1) instanceof ImmOperand immediate
            && immediate.getValue() != 0) count++;
      }
      int targetStart = instruction.getPredicate() == null ? 1 : 2;
      if (instruction.getOperands().size() >= targetStart + 2
          && instruction.getOperands().get(targetStart) instanceof BlockOperand ifTrue
          && instruction.getOperands().get(targetStart + 1) instanceof BlockOperand ifFalse
          && ifTrue.getBlock() != fallthrough && ifFalse.getBlock() != fallthrough) count++;
    }
    return count;
  }

  static int emittedBranchCount(MachineInstr instruction, MachineBasicBlock fallthrough) {
    if (instruction.getOpcode() == accela.backend.machine.MachineOpcode.BR) {
      return emittedInstructionCount(instruction, fallthrough);
    }
    if (instruction.getOpcode() != accela.backend.machine.MachineOpcode.CONDBR) return 1;
    int targetStart = instruction.getPredicate() == null ? 1 : 2;
    if (instruction.getOperands().size() < targetStart + 2
        || !(instruction.getOperands().get(targetStart) instanceof BlockOperand ifTrue)
        || !(instruction.getOperands().get(targetStart + 1) instanceof BlockOperand ifFalse)) {
      throw new IllegalStateException("malformed conditional Machine IR branch");
    }
    return ifTrue.getBlock() != fallthrough && ifFalse.getBlock() != fallthrough ? 2 : 1;
  }

  private static List<Set<MachineBasicBlock>> cyclicComponents(MachineFunction function) {
    Tarjan tarjan = new Tarjan(function);
    return tarjan.run().stream().filter(component -> component.size() > 1
        || successors(component.iterator().next()).contains(component.iterator().next())).toList();
  }

  private static List<MachineBasicBlock> successors(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return List.of();
    return block.getInstructions().getLast().getOperands().stream()
        .filter(BlockOperand.class::isInstance).map(BlockOperand.class::cast)
        .map(BlockOperand::getBlock).distinct().toList();
  }

  private static final class Tarjan {
    private final MachineFunction function;
    private final Map<MachineBasicBlock, Integer> indices = new IdentityHashMap<>();
    private final Map<MachineBasicBlock, Integer> lowLinks = new IdentityHashMap<>();
    private final ArrayDeque<MachineBasicBlock> stack = new ArrayDeque<>();
    private final Set<MachineBasicBlock> onStack =
        java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    private final List<Set<MachineBasicBlock>> components = new ArrayList<>();
    private int nextIndex;

    Tarjan(MachineFunction function) { this.function = function; }

    List<Set<MachineBasicBlock>> run() {
      for (MachineBasicBlock block : function.getBlocks()) {
        if (!indices.containsKey(block)) visit(block);
      }
      return List.copyOf(components);
    }

    private void visit(MachineBasicBlock block) {
      int index = nextIndex++;
      indices.put(block, index);
      lowLinks.put(block, index);
      stack.push(block);
      onStack.add(block);
      for (MachineBasicBlock successor : successors(block)) {
        if (!indices.containsKey(successor)) {
          visit(successor);
          lowLinks.put(block, Math.min(lowLinks.get(block), lowLinks.get(successor)));
        } else if (onStack.contains(successor)) {
          lowLinks.put(block, Math.min(lowLinks.get(block), indices.get(successor)));
        }
      }
      if (!lowLinks.get(block).equals(indices.get(block))) return;
      LinkedHashSet<MachineBasicBlock> component = new LinkedHashSet<>();
      MachineBasicBlock member;
      do {
        member = stack.pop();
        onStack.remove(member);
        component.add(member);
      } while (member != block);
      components.add(Set.copyOf(component));
    }
  }

  private record WeightedBlock(MachineBasicBlock block, double cost) {}

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

  static double curveEstimate(java.util.NavigableMap<Integer, Measurement> curve, int point) {
    int checkedPoint = Math.max(1, point);
    Map.Entry<Integer, Measurement> floor = curve.floorEntry(checkedPoint);
    Map.Entry<Integer, Measurement> ceiling = curve.ceilingEntry(checkedPoint);
    if (floor == null) {
      Map.Entry<Integer, Measurement> first = curve.firstEntry();
      return first.getValue().median() * checkedPoint / first.getKey();
    }
    if (ceiling != null) {
      if (floor.getKey().equals(ceiling.getKey())) return floor.getValue().median();
      double position = (double) (checkedPoint - floor.getKey())
          / (ceiling.getKey() - floor.getKey());
      return floor.getValue().median()
          + position * (ceiling.getValue().median() - floor.getValue().median());
    }
    Map.Entry<Integer, Measurement> last = curve.lastEntry();
    return last.getValue().median() * checkedPoint / last.getKey();
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
