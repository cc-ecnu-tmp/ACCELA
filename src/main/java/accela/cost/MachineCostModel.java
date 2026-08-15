package accela.cost;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.EnumMap;
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
    int instructionCount = 0;
    int codeBytes = 0;
    int branches = 0;
    int loads = 0;
    int stores = 0;
    double criticalPath = 0.0;
    double resourceCycles = 0.0;
    double variance = 0.0;
    EnumMap<InstructionClass, Integer> counts = new EnumMap<>(InstructionClass.class);

    for (MachineBasicBlock block : function.getBlocks()) {
      criticalPath += criticalPath(block);
      resourceCycles += simulate(block);
      for (MachineInstr instruction : block.getInstructions()) {
        InstructionClass instructionClass = InstructionClass.of(instruction.getOpcode());
        TargetProfile.OperationCost operation = profile.operation(instructionClass);
        counts.merge(instructionClass, 1, Integer::sum);
        instructionCount++;
        codeBytes += operation.codeBytes();
        if (instructionClass == InstructionClass.BRANCH
            || instructionClass == InstructionClass.CALL_RETURN) branches++;
        if (instructionClass == InstructionClass.LOAD) loads++;
        if (instructionClass == InstructionClass.STORE) stores++;
        variance += square(operation.latency().mad());
        variance += square(operation.reciprocalThroughput().mad());
      }
    }

    double frontend = Math.ceil((double) instructionCount / profile.fetchWidth());
    double memory =
        loads * profile.operation(InstructionClass.LOAD).reciprocalThroughput().median()
            + stores * profile.operation(InstructionClass.STORE).reciprocalThroughput().median();
    double branch = branches * profile.predictableBranch().median();
    AllocationEstimate allocation = allocator.estimate(function, target);
    double spill = allocation.spillWeight()
        * (profile.spillLoad().median() + profile.spillStore().median());
    double codeSize = codeBytes / (profile.fetchWidth() * 16.0);
    double bottleneck = Math.max(Math.max(criticalPath, frontend), Math.max(resourceCycles, memory));
    double cycles = bottleneck + branch + spill + codeSize;
    variance += branches * square(profile.predictableBranch().mad());
    variance += allocation.predictedSpills()
        * (square(profile.spillLoad().mad()) + square(profile.spillStore().mad()));
    return new CostEstimate(cycles, Math.sqrt(variance), criticalPath, frontend,
        resourceCycles, memory, branch, spill, codeSize);
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
