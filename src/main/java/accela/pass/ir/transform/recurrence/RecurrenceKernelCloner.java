package accela.pass.ir.transform.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.recurrence.RankedRecurrence;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Clones the recursive CFG as one table-cell kernel and replaces self calls with table reads. */
final class RecurrenceKernelCloner {
  private RecurrenceKernelCloner() {}

  static BasicBlock clone(
      RankedRecurrence recurrence,
      Function helper,
      List<Value> states,
      TabulationTable table,
      BasicBlock cellLatch,
      BasicBlock fallback) {
    Function source = recurrence.function();
    List<Integer> domainArguments = recurrence.domainArguments();
    Map<Value, Value> values = new IdentityHashMap<>();
    Map<BasicBlock, BasicBlock> blocks = new IdentityHashMap<>();
    for (int index = 0; index < source.getNumArgs(); index++) {
      Value replacement = helper.getArguments().get(index);
      int domainIndex = domainArguments.indexOf(index);
      if (domainIndex >= 0) replacement = states.get(domainIndex);
      values.put(source.getArguments().get(index), replacement);
    }
    for (BasicBlock block : source.getBlocks()) {
      BasicBlock copy = helper.addBlock("rrt.kernel." + block.getLabel());
      blocks.put(block, copy);
      values.put(block, copy);
    }

    Map<Instruction, Instruction> instructions = new IdentityHashMap<>();
    List<Instruction> calls = new ArrayList<>();
    List<Instruction> returns = new ArrayList<>();
    for (BasicBlock block : source.getBlocks()) {
      BasicBlock copyBlock = blocks.get(block);
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.RET) {
          returns.add(instruction);
          continue;
        }
        Instruction copy = instruction.copyWithoutOperands();
        copyBlock.addInstruction(copy);
        values.put(instruction, copy);
        instructions.put(instruction, copy);
        if (instruction.getOpcode() == Instruction.Opcode.CALL) calls.add(copy);
      }
    }
    for (var entry : instructions.entrySet()) {
      Instruction sourceInstruction = entry.getKey();
      Instruction copy = entry.getValue();
      for (int index = 0; index < sourceInstruction.getNumOperands(); index++) {
        copy.addOperand(values.getOrDefault(sourceInstruction.getOperand(index),
            sourceInstruction.getOperand(index)));
      }
    }
    for (Instruction sourceReturn : returns) {
      BasicBlock block = blocks.get(sourceReturn.getParent());
      IRBuilder builder = new IRBuilder(block);
      Value result = values.getOrDefault(sourceReturn.getOperand(0), sourceReturn.getOperand(0));
      builder.createStore(result, table.address(builder, states));
      builder.createBr(cellLatch);
    }
    for (Instruction call : calls) {
      replaceCall(call, recurrence, helper, table, fallback);
    }
    return blocks.get(source.getEntryBlock());
  }

  private static void replaceCall(
      Instruction call,
      RankedRecurrence recurrence,
      Function helper,
      TabulationTable table,
      BasicBlock fallback) {
    BasicBlock source = call.getParent();
    BasicBlock continuation =
        helper.insertBlockAfter(source, source.getLabel() + ".rrt.cont");
    moveTail(call, source, continuation);
    retargetSuccessorPhis(source, continuation);
    BasicBlock lookup = helper.insertBlockAfter(source, source.getLabel() + ".rrt.lookup");

    List<Value> children = recurrence.domainArguments().stream()
        .map(call::getOperand)
        .toList();
    List<Value> roots = recurrence.domainArguments().stream()
        .map(index -> (Value) helper.getArguments().get(index))
        .toList();
    IRBuilder builder = new IRBuilder(source);
    Value inBounds = stateInBounds(builder, children.getFirst(), roots.getFirst());
    for (int index = 1; index < children.size(); index++) {
      inBounds = builder.createAnd(
          inBounds,
          stateInBounds(builder, children.get(index), roots.get(index)));
    }
    builder.createCondBr(inBounds, lookup, fallback);

    builder.setInsertPoint(lookup);
    Value result =
        builder.createLoad(Type.INT, table.address(builder, children));
    builder.createBr(continuation);
    call.replaceAllUsesWith(result);
    call.eraseFromParent();
  }

  private static Value stateInBounds(IRBuilder builder, Value state, Value root) {
    return builder.createAnd(
        builder.createICmp("sge", state, Constant.intConst(0)),
        builder.createICmp("sle", state, root));
  }

  private static void moveTail(
      Instruction call, BasicBlock source, BasicBlock continuation) {
    List<Instruction> instructions = List.copyOf(source.getInstructions());
    for (int index = instructions.indexOf(call) + 1; index < instructions.size(); index++) {
      Instruction instruction = instructions.get(index);
      source.remove(instruction);
      continuation.addInstruction(instruction);
    }
  }

  private static void retargetSuccessorPhis(
      BasicBlock oldPredecessor, BasicBlock newPredecessor) {
    for (BasicBlock successor : newPredecessor.getSuccessors()) {
      for (Instruction phi : successor.getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        for (int index = 1; index < phi.getNumOperands(); index += 2) {
          if (phi.getOperand(index) == oldPredecessor) {
            phi.setOperand(index, newPredecessor);
          }
        }
      }
    }
  }
}
