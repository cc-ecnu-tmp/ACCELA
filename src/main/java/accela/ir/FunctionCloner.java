package accela.ir;

import java.util.IdentityHashMap;
import java.util.Map;

/** Clones a function body while preserving SSA and CFG references. */
public final class FunctionCloner {
  private FunctionCloner() {}

  public static Function cloneFunction(Function source, String name) {
    Function clone = new Function(name, source.getReturnType());
    Map<Value, Value> values = new IdentityHashMap<>();

    for (Function.Argument argument : source.getArguments()) {
      values.put(argument, clone.addArgument(argument.getType(), argument.getName()));
    }
    for (BasicBlock block : source.getBlocks()) {
      values.put(block, clone.addBlock(block.getName()));
    }

    for (BasicBlock block : source.getBlocks()) {
      BasicBlock clonedBlock = (BasicBlock) values.get(block);
      for (Instruction instruction : block.getInstructions()) {
        Instruction cloned = new Instruction(instruction.getOpcode(), instruction.getType());
        copyMetadata(instruction, cloned);
        clonedBlock.addInstruction(cloned);
        values.put(instruction, cloned);
      }
    }

    for (BasicBlock block : source.getBlocks()) {
      BasicBlock clonedBlock = (BasicBlock) values.get(block);
      for (int i = 0; i < block.getInstructions().size(); i++) {
        Instruction sourceInstruction = block.getInstructions().get(i);
        Instruction clonedInstruction = clonedBlock.getInstructions().get(i);
        for (int operand = 0; operand < sourceInstruction.getNumOperands(); operand++) {
          Value value = sourceInstruction.getOperand(operand);
          clonedInstruction.addOperand(values.getOrDefault(value, value));
        }
      }
    }
    return clone;
  }

  /** Clones one instruction's opcode and metadata without operands or parent block. */
  public static Instruction cloneInstruction(Instruction source) {
    Instruction clone = new Instruction(source.getOpcode(), source.getType());
    copyMetadata(source, clone);
    return clone;
  }

  private static void copyMetadata(Instruction source, Instruction clone) {
    clone.setName(source.getName());
    clone.setPredicate(source.getPredicate());
    clone.setAllocatedType(source.getAllocatedType());
    clone.setGepSourceType(source.getGepSourceType());
    clone.setGepInbounds(source.isGepInbounds());
    clone.setCallee(source.getCallee());
  }
}
