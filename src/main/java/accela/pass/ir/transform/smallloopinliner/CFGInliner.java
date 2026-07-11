package accela.pass.ir.transform.smallloopinliner;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.FunctionCloner;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Clones one direct call's CFG into its caller. */
final class CFGInliner {
  private static int nextId;
  private CFGInliner() {}

  static void inline(Instruction call) {
    Function callee = call.getCallee();
    BasicBlock callerBlock = call.getParent();
    Function caller = callerBlock.getParent();
    int id = nextId++;
    BasicBlock continuation = caller.insertBlockAfter(
        callerBlock, callerBlock.getLabel() + ".inline.cont." + id);
    moveTail(call, callerBlock, continuation);
    retargetSuccessorPhis(callerBlock, continuation);

    Map<Value, Value> values = new IdentityHashMap<>();
    for (int index = 0; index < callee.getNumArgs(); index++) {
      values.put(callee.getArguments().get(index), call.getOperand(index));
    }
    BasicBlock insertionPoint = callerBlock;
    for (BasicBlock source : callee.getBlocks()) {
      BasicBlock clone = caller.insertBlockAfter(
          insertionPoint, source.getLabel() + ".inline." + id);
      values.put(source, clone);
      insertionPoint = clone;
    }

    Map<Instruction, Instruction> instructions = new IdentityHashMap<>();
    for (BasicBlock sourceBlock : callee.getBlocks()) {
      BasicBlock cloneBlock = (BasicBlock) values.get(sourceBlock);
      for (Instruction source : sourceBlock.getInstructions()) {
        if (source.getOpcode() == Instruction.Opcode.RET) continue;
        Instruction clone = FunctionCloner.cloneInstruction(source);
        cloneBlock.addInstruction(clone);
        values.put(source, clone);
        instructions.put(source, clone);
      }
    }
    for (Map.Entry<Instruction, Instruction> entry : instructions.entrySet()) {
      Instruction source = entry.getKey();
      Instruction clone = entry.getValue();
      for (int index = 0; index < source.getNumOperands(); index++) {
        Value operand = source.getOperand(index);
        clone.addOperand(values.getOrDefault(operand, operand));
      }
    }

    List<Value> returns = new ArrayList<>();
    List<BasicBlock> returnBlocks = new ArrayList<>();
    for (BasicBlock sourceBlock : callee.getBlocks()) {
      Instruction terminator = sourceBlock.getTerminator();
      if (terminator.getOpcode() != Instruction.Opcode.RET) continue;
      BasicBlock cloneBlock = (BasicBlock) values.get(sourceBlock);
      if (terminator.getNumOperands() == 1) {
        Value result = terminator.getOperand(0);
        returns.add(values.getOrDefault(result, result));
        returnBlocks.add(cloneBlock);
      }
      new IRBuilder(cloneBlock).createBr(continuation);
    }
    new IRBuilder(callerBlock).createBr((BasicBlock) values.get(callee.getEntryBlock()));

    if (!returns.isEmpty()) {
      Value result = returns.getFirst();
      if (returns.size() > 1) {
        Instruction phi = Instruction.createPhi(call.getType());
        continuation.addInstructionToFront(phi);
        for (int index = 0; index < returns.size(); index++) {
          phi.addOperand(returns.get(index));
          phi.addOperand(returnBlocks.get(index));
        }
        result = phi;
      }
      call.replaceAllUsesWith(result);
    }
    call.eraseFromParent();
  }
  private static void moveTail(
      Instruction call, BasicBlock source, BasicBlock continuation) {
    List<Instruction> instructions = List.copyOf(source.getInstructions());
    int callIndex = instructions.indexOf(call);
    for (int index = callIndex + 1; index < instructions.size(); index++) {
      Instruction instruction = instructions.get(index);
      source.remove(instruction);
      continuation.addInstruction(instruction);
    }
  }

  private static void retargetSuccessorPhis(
      BasicBlock oldPredecessor, BasicBlock newPredecessor) {
    for (BasicBlock successor : newPredecessor.getSuccessors()) {
      for (Instruction instruction : successor.getInstructions()) {
        if (instruction.getOpcode() != Instruction.Opcode.PHI) break;
        for (int index = 1; index < instruction.getNumOperands(); index += 2) {
          if (instruction.getOperand(index) == oldPredecessor) {
            instruction.setOperand(index, newPredecessor);
          }
        }
      }
    }
  }
}
