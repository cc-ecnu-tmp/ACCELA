package accela.backend.regalloc;

import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class AllocatorState {
  private final InterferenceGraph graph;
  private final TargetRegisterInfo registers;
  private final SpillCostModel spillCostModel;
  private final Set<VirtualRegister> liveAcrossCall;
  private final Set<VirtualRegister> argumentRegisterHazards;

  final Set<VirtualRegister> initial = new HashSet<>();
  final Set<VirtualRegister> simplifyWorklist = new HashSet<>();
  final Set<VirtualRegister> freezeWorklist = new HashSet<>();
  final Set<VirtualRegister> spillWorklist = new HashSet<>();

  final Deque<VirtualRegister> selectStack = new ArrayDeque<>();

  final Set<InterferenceGraphBuilder.Move> worklistMoves = new HashSet<>();
  final Set<InterferenceGraphBuilder.Move> activeMoves = new HashSet<>();
  final Set<InterferenceGraphBuilder.Move> coalescedMoves = new HashSet<>();
  final Set<InterferenceGraphBuilder.Move> constrainedMoves = new HashSet<>();
  final Set<InterferenceGraphBuilder.Move> frozenMoves = new HashSet<>();

  final Set<VirtualRegister> spilledNodes = new HashSet<>();
  final Set<VirtualRegister> coalescedNodes = new HashSet<>();
  final Set<VirtualRegister> coloredNodes = new HashSet<>();

  final Map<VirtualRegister, Integer> degree = new HashMap<>();
  final Map<VirtualRegister, Set<InterferenceGraphBuilder.Move>> moveList = new HashMap<>();
  final Map<VirtualRegister, VirtualRegister> alias = new HashMap<>();
  final Map<VirtualRegister, PhysicalRegister> color = new HashMap<>();

  AllocatorState(InterferenceGraphBuilder.Result built, TargetRegisterInfo registers) {
    this(built, registers, ignored -> 1.0, Collections.emptySet(), Collections.emptySet());
  }

  AllocatorState(
      InterferenceGraphBuilder.Result built,
      TargetRegisterInfo registers,
      SpillCostModel spillCostModel) {
    this(built, registers, spillCostModel, Collections.emptySet(), Collections.emptySet());
  }

  AllocatorState(
      InterferenceGraphBuilder.Result built,
      TargetRegisterInfo registers,
      SpillCostModel spillCostModel,
      Set<VirtualRegister> liveAcrossCall) {
    this(built, registers, spillCostModel, liveAcrossCall, Collections.emptySet());
  }

  AllocatorState(
      InterferenceGraphBuilder.Result built,
      TargetRegisterInfo registers,
      SpillCostModel spillCostModel,
      Set<VirtualRegister> liveAcrossCall,
      Set<VirtualRegister> argumentRegisterHazards) {
    this.graph = built.graph();
    this.registers = registers;
    this.spillCostModel = spillCostModel;
    this.liveAcrossCall = new HashSet<>(liveAcrossCall);
    this.argumentRegisterHazards = new HashSet<>(argumentRegisterHazards);

    initial.addAll(graph.nodes());
    worklistMoves.addAll(built.moves());

    for (VirtualRegister register : graph.nodes()) {
      degree.put(register, graph.degree(register));
    }

    for (InterferenceGraphBuilder.Move move : built.moves()) {
      addMove(move.src(), move);
      addMove(move.dst(), move);
    }
  }

  void makeWorklist() {
    for (VirtualRegister register : initial) {
      if (degree(register) >= registerCount(register)) {
        spillWorklist.add(register);
      } else if (moveRelated(register)) {
        freezeWorklist.add(register);
      } else {
        simplifyWorklist.add(register);
      }
    }
    initial.clear();
  }

  boolean moveRelated(VirtualRegister register) {
    return !nodeMoves(register).isEmpty();
  }

  Set<InterferenceGraphBuilder.Move> nodeMoves(VirtualRegister register) {
    Set<InterferenceGraphBuilder.Move> result =
        new HashSet<>(moveList.getOrDefault(register, Collections.emptySet()));
    Set<InterferenceGraphBuilder.Move> availableMoves = new HashSet<>(worklistMoves);
    availableMoves.addAll(activeMoves);
    result.retainAll(availableMoves);
    return result;
  }

  int degree(VirtualRegister register) {
    return degree.getOrDefault(register, 0);
  }

  private int registerCount(VirtualRegister register) {
    return candidateRegisters(register).size();
  }

  private int registerCountAfterCoalescing(
      VirtualRegister representative, VirtualRegister merged) {
    if (liveAcrossCall.contains(representative) || liveAcrossCall.contains(merged)) {
      return registers.calleeSavedRegisters(representative.getType()).size();
    }
    if (argumentRegisterHazards.contains(representative)
        || argumentRegisterHazards.contains(merged)) {
      return registers.nonArgumentRegisters(representative.getType()).size();
    }
    return registers.allocatableRegisters(representative.getType()).size();
  }

  private void addMove(VirtualRegister register, InterferenceGraphBuilder.Move move) {
    moveList.computeIfAbsent(register, ignored -> new HashSet<>()).add(move);
  }

  public static VirtualRegister getAndRemoveOne(Set<VirtualRegister> it){
    var res = it.iterator().next();
    it.remove(res);
    return res;
  }

  Set<VirtualRegister> getAdjacent(VirtualRegister it){
    Set<VirtualRegister> res = new HashSet<>(graph.neighbors(it));
    res.removeAll(selectStack);
    res.removeAll(coalescedNodes);
    return res;
  }

  void simplify(){
    VirtualRegister register = getAndRemoveOne(simplifyWorklist);
    selectStack.push(register);

    for (VirtualRegister neighbor : getAdjacent(register)) {
      decrementDegree(neighbor);
    }
  }

  void decrementDegree(VirtualRegister register){
    int oldDegree = degree(register);
    degree.put(register,oldDegree-1);
    
    if (oldDegree == registerCount(register)) {
      Set<VirtualRegister> affected = getAdjacent(register);
      affected.add(register);
      enableMoves(affected);

      spillWorklist.remove(register);
      if (moveRelated(register)) {
        freezeWorklist.add(register);
      } else {
        simplifyWorklist.add(register);
      }
    }
  }

  void enableMoves(Set<VirtualRegister> registers){
    for (VirtualRegister register : registers) {
      for (InterferenceGraphBuilder.Move move : nodeMoves(register)) {
        if (activeMoves.remove(move)) {
          worklistMoves.add(move);
        }
      }
    }
  }

  void coalesce() {
    InterferenceGraphBuilder.Move move = getAndRemoveOneMove(worklistMoves);
    VirtualRegister src = getAlias(move.src());
    VirtualRegister dst = getAlias(move.dst());

    if (src.getType() != dst.getType()) {
      constrainedMoves.add(move);
      addWorkList(src);
      addWorkList(dst);
      return;
    }

    if (src.equals(dst)) {
      coalescedMoves.add(move);
      addWorkList(src);
      return;
    }

    if (graph.interferes(src, dst)) {
      constrainedMoves.add(move);
      addWorkList(src);
      addWorkList(dst);
      return;
    }

    Set<VirtualRegister> combinedAdjacent = getAdjacent(src);
    combinedAdjacent.addAll(getAdjacent(dst));
    if (conservative(combinedAdjacent, registerCountAfterCoalescing(src, dst))) {
      coalescedMoves.add(move);
      combine(src, dst);
      addWorkList(src);
    } else {
      activeMoves.add(move);
    }
  }

  void freeze() {
    VirtualRegister register = getAndRemoveOne(freezeWorklist);
    simplifyWorklist.add(register);
    freezeMoves(register);
  }

  void freezeMoves(VirtualRegister register) {
    for (InterferenceGraphBuilder.Move move : nodeMoves(register)) {
      VirtualRegister src = getAlias(move.src());
      VirtualRegister dst = getAlias(move.dst());
      VirtualRegister other = getAlias(register).equals(src) ? dst : src;

      activeMoves.remove(move);
      worklistMoves.remove(move);
      frozenMoves.add(move);

      if (!moveRelated(other) && degree(other) < registerCount(other)) {
        freezeWorklist.remove(other);
        simplifyWorklist.add(other);
      }
    }
  }

  void selectSpill() {
    VirtualRegister selected = null;

    for (VirtualRegister register : spillWorklist) {
      if (selected == null
          || spillCostModel.cost(register, degree(register))
              < spillCostModel.cost(selected, degree(selected))) {
        selected = register;
      }
    }

    spillWorklist.remove(selected);
    simplifyWorklist.add(selected);
    freezeMoves(selected);
  }

  void assignColors() {
    while (!selectStack.isEmpty()) {
      VirtualRegister register = selectStack.pop();
      Set<String> unavailable = new HashSet<>();

      for (VirtualRegister neighbor : graph.neighbors(register)) {
        VirtualRegister aliasNeighbor = getAlias(neighbor);
        PhysicalRegister assigned = color.get(aliasNeighbor);
        if (assigned != null) {
          unavailable.add(assigned.getName());
        }
      }

      PhysicalRegister selected = null;
      List<PhysicalRegister> candidates = candidateRegisters(register);
      for (PhysicalRegister candidate : candidates) {
        if (!unavailable.contains(candidate.getName())) {
          selected = candidate;
          break;
        }
      }

      if (selected == null) {
        spilledNodes.add(register);
      } else {
        coloredNodes.add(register);
        color.put(register, selected);
      }
    }

    for (VirtualRegister register : coalescedNodes) {
      VirtualRegister representative = getAlias(register);
      PhysicalRegister assigned = color.get(representative);
      if (assigned != null) {
        color.put(register, assigned);
      } else if (spilledNodes.contains(representative)) {
        spilledNodes.add(register);
      }
    }
  }

  private List<PhysicalRegister> candidateRegisters(VirtualRegister register) {
    if (liveAcrossCall.contains(getAlias(register))) {
      return registers.calleeSavedRegisters(register.getType());
    }
    if (argumentRegisterHazards.contains(getAlias(register))) {
      return registers.nonArgumentRegisters(register.getType());
    }
    return registers.allocatableRegisters(register.getType());
  }

  VirtualRegister getAlias(VirtualRegister register) {
    if (coalescedNodes.contains(register)) {
      return getAlias(alias.get(register));
    }
    return register;
  }

  void addWorkList(VirtualRegister register) {
    if (!moveRelated(register) && degree(register) < registerCount(register)) {
      freezeWorklist.remove(register);
      simplifyWorklist.add(register);
    }
  }

  boolean conservative(Set<VirtualRegister> nodes, int registerCount) {
    int highDegreeCount = 0;
    for (VirtualRegister node : nodes) {
      if (degree(node) >= registerCount) {
        highDegreeCount++;
      }
    }
    return highDegreeCount < registerCount;
  }

  void combine(VirtualRegister representative, VirtualRegister merged) {
    freezeWorklist.remove(merged);
    spillWorklist.remove(merged);
    coalescedNodes.add(merged);
    alias.put(merged, representative);
    spillCostModel.combine(representative, merged);
    if (liveAcrossCall.contains(merged)) {
      liveAcrossCall.add(representative);
    }
    if (argumentRegisterHazards.contains(merged)) {
      argumentRegisterHazards.add(representative);
    }

    moveList
        .computeIfAbsent(representative, ignored -> new HashSet<>())
        .addAll(moveList.getOrDefault(merged, Collections.emptySet()));

    enableMoves(Set.of(merged));

    for (VirtualRegister neighbor : getAdjacent(merged)) {
      boolean alreadyInterferes = graph.interferes(neighbor, representative);
      graph.addEdge(neighbor, representative);
      if (alreadyInterferes) {
        decrementDegree(neighbor);
      } else {
        degree.put(representative, degree(representative) + 1);
      }
    }

    if (degree(representative) >= registerCount(representative)
        && freezeWorklist.remove(representative)) {
      spillWorklist.add(representative);
    }
  }

  private static InterferenceGraphBuilder.Move getAndRemoveOneMove(
      Set<InterferenceGraphBuilder.Move> moves) {
    InterferenceGraphBuilder.Move move = moves.iterator().next();
    moves.remove(move);
    return move;
  }
}
