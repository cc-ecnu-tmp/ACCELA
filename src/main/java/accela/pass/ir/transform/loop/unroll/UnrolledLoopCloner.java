package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Builds and connects every iteration of a constant-trip loop. */
final class UnrolledLoopCloner {
  record Result(
      BasicBlock entry,
      Map<Instruction, Value> finalHeaderValues) {}

  private static int nextId;

  private UnrolledLoopCloner() {}

  static Result clone(Function function, LoopUnrollCandidate candidate) {
    List<BasicBlock> sources = function.getBlocks().stream()
        .filter(candidate.loop()::contains)
        .toList();
    List<Map<BasicBlock, BasicBlock>> blocks =
        createBlocks(function, sources, candidate.tripCount());
    Map<Instruction, Value> carried = headerValues(
        candidate, null, candidate.induction().predecessor());
    for (int iteration = 0; iteration < candidate.tripCount(); iteration++) {
      Map<Value, Value> values = LoopIterationCloner.clone(
          candidate, sources, blocks, iteration, carried, candidate.exit());
      carried = headerValues(candidate, values, candidate.induction().latch());
    }
    return new Result(
        blocks.getFirst().get(candidate.loop().header()),
        Map.copyOf(carried));
  }

  private static List<Map<BasicBlock, BasicBlock>> createBlocks(
      Function function, List<BasicBlock> sources, int tripCount) {
    int id = nextId++;
    BasicBlock insertionPoint = sources.getLast();
    List<Map<BasicBlock, BasicBlock>> iterations = new ArrayList<>();
    for (int iteration = 0; iteration < tripCount; iteration++) {
      Map<BasicBlock, BasicBlock> blocks = new IdentityHashMap<>();
      for (BasicBlock source : sources) {
        BasicBlock copy = function.insertBlockAfter(
            insertionPoint, source.getLabel() + ".unroll." + id + "." + iteration);
        blocks.put(source, copy);
        insertionPoint = copy;
      }
      iterations.add(blocks);
    }
    return iterations;
  }

  private static Map<Instruction, Value> headerValues(
      LoopUnrollCandidate candidate,
      Map<Value, Value> values,
      BasicBlock predecessor) {
    Map<Instruction, Value> result = new IdentityHashMap<>();
    for (Instruction phi : candidate.loop().header().getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value incoming = incomingValue(phi, predecessor);
      result.put(phi, values == null ? incoming : values.getOrDefault(incoming, incoming));
    }
    return result;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    throw new IllegalStateException("missing loop PHI incoming value");
  }
}
