package accela.backend.regalloc;

import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.ToIntFunction;

/** Briggs-style optimistic simplify/select graph coloring. */
final class OptimisticGraphColoring {
  private OptimisticGraphColoring() {}

  /** Returns color indexes, using -1 for nodes that are actual spills during select. */
  static Map<VirtualRegister, Integer> color(
      Collection<VirtualRegister> registers,
      InterferenceGraph graph,
      int colorCount,
      ToIntFunction<VirtualRegister> spillWeight) {
    if (colorCount <= 0) throw new IllegalArgumentException("colorCount must be positive");
    Set<VirtualRegister> remaining = identitySet();
    remaining.addAll(registers);
    List<VirtualRegister> stack = new ArrayList<>();

    while (!remaining.isEmpty()) {
      VirtualRegister selected = chooseLowDegree(remaining, graph, colorCount);
      if (selected == null) {
        selected = choosePotentialSpill(remaining, graph, spillWeight);
      }
      stack.add(selected);
      remaining.remove(selected);
    }

    Map<VirtualRegister, Integer> colors = new IdentityHashMap<>();
    for (int i = stack.size() - 1; i >= 0; i--) {
      VirtualRegister register = stack.get(i);
      boolean[] unavailable = new boolean[colorCount];
      for (VirtualRegister neighbor : graph.neighbors(register)) {
        Integer color = colors.get(neighbor);
        if (color != null && color >= 0) unavailable[color] = true;
      }
      int color = 0;
      while (color < colorCount && unavailable[color]) color++;
      colors.put(register, color < colorCount ? color : -1);
    }
    return colors;
  }

  private static VirtualRegister chooseLowDegree(
      Set<VirtualRegister> remaining, InterferenceGraph graph, int colorCount) {
    return remaining.stream()
        .filter(register -> degree(register, remaining, graph) < colorCount)
        .min(Comparator.comparingInt(VirtualRegister::getId))
        .orElse(null);
  }

  private static VirtualRegister choosePotentialSpill(
      Set<VirtualRegister> remaining,
      InterferenceGraph graph,
      ToIntFunction<VirtualRegister> spillWeight) {
    return remaining.stream().min((first, second) -> {
      long firstCost = Math.max(1, spillWeight.applyAsInt(first));
      long secondCost = Math.max(1, spillWeight.applyAsInt(second));
      long firstDegree = Math.max(1, degree(first, remaining, graph));
      long secondDegree = Math.max(1, degree(second, remaining, graph));
      int comparison = Long.compare(firstCost * secondDegree, secondCost * firstDegree);
      return comparison != 0 ? comparison : Integer.compare(first.getId(), second.getId());
    }).orElseThrow();
  }

  private static int degree(
      VirtualRegister register, Set<VirtualRegister> remaining, InterferenceGraph graph) {
    int degree = 0;
    for (VirtualRegister neighbor : graph.neighbors(register)) {
      if (remaining.contains(neighbor)) degree++;
    }
    return degree;
  }

  private static Set<VirtualRegister> identitySet() {
    return Collections.newSetFromMap(new IdentityHashMap<>());
  }
}
