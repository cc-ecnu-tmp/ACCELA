package accela.backend.regalloc;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Undirected virtual-register interference derived from instruction liveness. */
final class InterferenceGraph {
  private final Map<VirtualRegister, Set<VirtualRegister>> edges = new IdentityHashMap<>();

  static InterferenceGraph build(MachineFunction function, LivenessAnalysis.Result liveness) {
    InterferenceGraph graph = new InterferenceGraph();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        graph.addClique(liveness.liveBefore(instruction));
        graph.addClique(liveness.liveAfter(instruction));
      }
    }
    return graph;
  }

  boolean interferes(VirtualRegister first, VirtualRegister second) {
    return edges.getOrDefault(first, Collections.emptySet()).contains(second);
  }

  Set<VirtualRegister> neighbors(VirtualRegister register) {
    return Collections.unmodifiableSet(edges.getOrDefault(register, Collections.emptySet()));
  }

  private void addClique(Set<VirtualRegister> live) {
    List<VirtualRegister> registers = new ArrayList<>(live);
    for (int i = 0; i < registers.size(); i++) {
      for (int j = i + 1; j < registers.size(); j++) {
        addEdge(registers.get(i), registers.get(j));
      }
    }
  }

  private void addEdge(VirtualRegister first, VirtualRegister second) {
    edges.computeIfAbsent(first, unused -> identitySet()).add(second);
    edges.computeIfAbsent(second, unused -> identitySet()).add(first);
  }

  private static Set<VirtualRegister> identitySet() {
    return Collections.newSetFromMap(new IdentityHashMap<>());
  }
}
