package accela.ir;

import java.util.IdentityHashMap;
import java.util.Map;

/** Lossless deep-copy support used by transactional IR candidate evaluation. */
public final class IRSnapshot {
  private IRSnapshot() {}

  public static Module deepCopy(Module source) {
    Module copy = new Module();
    Map<Value, Value> values = new IdentityHashMap<>();

    for (GlobalVariable global : source.getGlobals()) {
      GlobalVariable cloned = new GlobalVariable(global.getName(), global.getValueType(),
          global.getInitializer(), global.isConstant());
      copy.addGlobal(cloned);
      values.put(global, cloned);
    }
    for (Function declaration : source.getDeclares()) {
      Function cloned = copyFunctionHeader(declaration, values);
      copy.addDeclare(cloned);
    }
    for (Function function : source.getFunctions()) {
      Function cloned = copyFunctionHeader(function, values);
      copy.addFunction(cloned);
    }

    for (int functionIndex = 0; functionIndex < source.getFunctions().size(); functionIndex++) {
      Function original = source.getFunctions().get(functionIndex);
      Function cloned = copy.getFunctions().get(functionIndex);
      for (BasicBlock block : original.getBlocks()) {
        BasicBlock clonedBlock = cloned.addBlock(block.getLabel());
        values.put(block, clonedBlock);
      }
      for (int blockIndex = 0; blockIndex < original.getBlocks().size(); blockIndex++) {
        BasicBlock block = original.getBlocks().get(blockIndex);
        BasicBlock clonedBlock = cloned.getBlocks().get(blockIndex);
        for (Instruction instruction : block.getInstructions()) {
          Instruction clonedInstruction = instruction.copyWithoutOperands();
          clonedBlock.addInstruction(clonedInstruction);
          values.put(instruction, clonedInstruction);
        }
      }
    }

    for (int functionIndex = 0; functionIndex < source.getFunctions().size(); functionIndex++) {
      Function original = source.getFunctions().get(functionIndex);
      Function cloned = copy.getFunctions().get(functionIndex);
      for (int blockIndex = 0; blockIndex < original.getBlocks().size(); blockIndex++) {
        BasicBlock block = original.getBlocks().get(blockIndex);
        BasicBlock clonedBlock = cloned.getBlocks().get(blockIndex);
        for (int instructionIndex = 0; instructionIndex < block.getInstructions().size(); instructionIndex++) {
          Instruction instruction = block.getInstructions().get(instructionIndex);
          Instruction clonedInstruction = clonedBlock.getInstructions().get(instructionIndex);
          for (int operandIndex = 0; operandIndex < instruction.getNumOperands(); operandIndex++) {
            Value operand = instruction.getOperand(operandIndex);
            clonedInstruction.addOperand(values.getOrDefault(operand, operand));
          }
          if (instruction.getCallee() != null) {
            Value callee = values.get(instruction.getCallee());
            if (!(callee instanceof Function function)) {
              throw new IllegalStateException("IR snapshot cannot resolve call target "
                  + instruction.getCallee().getName());
            }
            clonedInstruction.setCallee(function);
          }
        }
      }
    }
    return copy;
  }

  private static Function copyFunctionHeader(Function source, Map<Value, Value> values) {
    Function copy = new Function(source.getName(), source.getReturnType());
    values.put(source, copy);
    for (Function.Argument argument : source.getArguments()) {
      Function.Argument cloned = copy.addArgument(argument.getType(), argument.getName());
      values.put(argument, cloned);
    }
    return copy;
  }
}
