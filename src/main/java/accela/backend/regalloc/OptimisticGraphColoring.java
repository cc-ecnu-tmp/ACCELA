package accela.backend.regalloc;

import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Set;
import java.util.function.BiPredicate;
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
    return color(registers, graph, colorCount, spillWeight, (register, color) -> true);
  }

  static Map<VirtualRegister, Integer> color(
      Collection<VirtualRegister> registers,
      InterferenceGraph graph,
      int colorCount,
      ToIntFunction<VirtualRegister> spillWeight,
      BiPredicate<VirtualRegister, Integer> colorAllowed) {
    if (colorCount <= 0) throw new IllegalArgumentException("colorCount must be positive");
    Set<VirtualRegister> remaining = identitySet();
    remaining.addAll(registers);
    List<VirtualRegister> stack = new ArrayList<>();
    Map<VirtualRegister, Integer> degrees = new IdentityHashMap<>();
    PriorityQueue<VirtualRegister> lowDegree =
        new PriorityQueue<>(Comparator.comparingInt(VirtualRegister::getId));
    for (VirtualRegister register : remaining) {
      int degree = currentDegree(register, remaining, graph);
      degrees.put(register, degree);
      if (degree < colorCount) lowDegree.add(register);
    }

    while (!remaining.isEmpty()) {
      VirtualRegister selected = pollLowDegree(lowDegree, remaining, degrees, colorCount);
      if (selected == null) {
        selected = choosePotentialSpill(remaining, degrees, spillWeight);
      }
      stack.add(selected);
      remaining.remove(selected);
      for (VirtualRegister neighbor : graph.neighbors(selected)) {
        if (!remaining.contains(neighbor)) continue;
        int degree = degrees.get(neighbor) - 1;
        degrees.put(neighbor, degree);
        if (degree == colorCount - 1) lowDegree.add(neighbor);
      }
    }

    Map<VirtualRegister, Integer> colors = new IdentityHashMap<>();
    for (int i = stack.size() - 1; i >= 0; i--) {
      VirtualRegister register = stack.get(i);
      boolean[] unavailable = new boolean[colorCount];
      for (int color = 0; color < colorCount; color++) {
        unavailable[color] = !colorAllowed.test(register, color);
      }
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

  private static VirtualRegister pollLowDegree(
      PriorityQueue<VirtualRegister> lowDegree,
      Set<VirtualRegister> remaining,
      Map<VirtualRegister, Integer> degrees,
      int colorCount) {
    while (!lowDegree.isEmpty()) {
      VirtualRegister register = lowDegree.remove();
      if (remaining.contains(register) && degrees.get(register) < colorCount) return register;
    }
    return null;
  }

  private static VirtualRegister choosePotentialSpill(
      Set<VirtualRegister> remaining,
      Map<VirtualRegister, Integer> degrees,
      ToIntFunction<VirtualRegister> spillWeight) {
    return remaining.stream().min((first, second) -> {
      long firstCost = Math.max(1, spillWeight.applyAsInt(first));
      long secondCost = Math.max(1, spillWeight.applyAsInt(second));
      long firstDegree = Math.max(1, degrees.get(first));
      long secondDegree = Math.max(1, degrees.get(second));
      int comparison = Long.compare(firstCost * secondDegree, secondCost * firstDegree);
      return comparison != 0 ? comparison : Integer.compare(first.getId(), second.getId());
    }).orElseThrow();
  }

  private static int currentDegree(
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
