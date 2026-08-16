package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Clones one loop iteration with header PHIs replaced by carried values. */
final class LoopIterationCloner {
  private LoopIterationCloner() {}

  static Map<Value, Value> clone(
      LoopUnrollCandidate candidate,
      List<BasicBlock> sources,
      List<Map<BasicBlock, BasicBlock>> iterations,
      int iteration,
      Map<Instruction, Value> carried,
      BasicBlock finalTarget) {
    Map<BasicBlock, BasicBlock> blocks = iterations.get(iteration);
    Map<Value, Value> values = new IdentityHashMap<>();
    values.putAll(carried);
    Map<Instruction, Instruction> instructions = new IdentityHashMap<>();
    for (BasicBlock sourceBlock : sources) {
      for (Instruction source : sourceBlock.getInstructions()) {
        if (isReplaced(source, sourceBlock, candidate)) continue;
        Instruction copy = source.copyWithoutOperands();
        blocks.get(sourceBlock).addInstruction(copy);
        values.put(source, copy);
        instructions.put(source, copy);
      }
    }
    for (var entry : instructions.entrySet()) {
      Instruction source = entry.getKey();
      Instruction copy = entry.getValue();
      for (int index = 0; index < source.getNumOperands(); index++) {
        copy.addOperand(remap(source.getOperand(index), values, blocks));
      }
    }

    new IRBuilder(blocks.get(candidate.loop().header()))
        .createBr(blocks.get(candidate.body()));
    BasicBlock next = iteration + 1 < iterations.size()
        ? iterations.get(iteration + 1).get(candidate.loop().header())
        : finalTarget;
    new IRBuilder(blocks.get(candidate.induction().latch())).createBr(next);
    return values;
  }

  private static boolean isReplaced(
      Instruction instruction,
      BasicBlock block,
      LoopUnrollCandidate candidate) {
    if (block == candidate.loop().header()) {
      return instruction.getOpcode() == Instruction.Opcode.PHI
          || instruction == candidate.compare()
          || instruction == block.getTerminator();
    }
    return block == candidate.induction().latch()
        && instruction == block.getTerminator();
  }

  private static Value remap(
      Value value,
      Map<Value, Value> values,
      Map<BasicBlock, BasicBlock> blocks) {
    if (value instanceof BasicBlock block) return blocks.getOrDefault(block, block);
    return values.getOrDefault(value, value);
  }
}
