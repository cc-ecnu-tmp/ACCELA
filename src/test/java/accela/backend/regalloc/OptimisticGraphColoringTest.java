package accela.backend.regalloc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.MachineType;
import accela.backend.machine.VirtualRegister;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

final class OptimisticGraphColoringTest {
  @Test
  void colorsAHighDegreeEvenCycleWithoutPessimisticSpills() {
    List<VirtualRegister> registers = registers(4);
    InterferenceGraph graph = new InterferenceGraph();
    graph.addEdge(registers.get(0), registers.get(1));
    graph.addEdge(registers.get(1), registers.get(2));
    graph.addEdge(registers.get(2), registers.get(3));
    graph.addEdge(registers.get(3), registers.get(0));

    Map<VirtualRegister, Integer> colors =
        OptimisticGraphColoring.color(registers, graph, 2, unused -> 1);

    assertTrue(colors.values().stream().allMatch(color -> color >= 0));
    for (int i = 0; i < registers.size(); i++) {
      assertNotEquals(colors.get(registers.get(i)), colors.get(registers.get((i + 1) % 4)));
    }
  }

  @Test
  void spillsOnlyOneNodeFromAThreeCliqueWithTwoColors() {
    List<VirtualRegister> registers = registers(3);
    InterferenceGraph graph = clique(registers);

    Map<VirtualRegister, Integer> colors =
        OptimisticGraphColoring.color(registers, graph, 2, unused -> 1);

    assertEquals(1, colors.values().stream().filter(color -> color < 0).count());
  }

  @Test
  void choosesTheCheaperPotentialSpill() {
    List<VirtualRegister> registers = registers(2);
    InterferenceGraph graph = clique(registers);

    Map<VirtualRegister, Integer> colors =
        OptimisticGraphColoring.color(
            registers, graph, 1, register -> register == registers.get(0) ? 1 : 100);

    assertEquals(-1, colors.get(registers.get(0)));
    assertEquals(0, colors.get(registers.get(1)));
  }

  @Test
  void respectsPerNodeColorConstraints() {
    List<VirtualRegister> registers = registers(2);
    InterferenceGraph graph = new InterferenceGraph();

    Map<VirtualRegister, Integer> colors =
        OptimisticGraphColoring.color(
            registers, graph, 2, unused -> 1,
            (register, color) -> register == registers.get(0) ? color == 1 : false);

    assertEquals(1, colors.get(registers.get(0)));
    assertEquals(-1, colors.get(registers.get(1)));
  }

  @Test
  void usesAnAvailablePreferredColor() {
    List<VirtualRegister> registers = registers(3);
    InterferenceGraph graph = new InterferenceGraph();
    graph.addEdge(registers.get(1), registers.get(2));

    Map<VirtualRegister, Integer> colors =
        OptimisticGraphColoring.color(
            registers, graph, 2, unused -> 1, (register, color) -> true,
            register -> register == registers.get(0)
                ? List.of(registers.get(1)) : List.of());

    assertEquals(colors.get(registers.get(1)), colors.get(registers.get(0)));
  }

  private static List<VirtualRegister> registers(int count) {
    return java.util.stream.IntStream.range(0, count)
        .mapToObj(id -> new VirtualRegister(id, MachineType.I32, "v" + id))
        .toList();
  }

  private static InterferenceGraph clique(List<VirtualRegister> registers) {
    InterferenceGraph graph = new InterferenceGraph();
    for (int i = 0; i < registers.size(); i++) {
      for (int j = i + 1; j < registers.size(); j++) {
        graph.addEdge(registers.get(i), registers.get(j));
      }
    }
    return graph;
  }
}
