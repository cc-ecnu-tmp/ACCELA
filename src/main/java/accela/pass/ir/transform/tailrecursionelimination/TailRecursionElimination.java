package accela.pass.ir.transform.tailrecursionelimination;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayList;
import java.util.List;

/** Turns direct self calls returned immediately by their caller into loop backedges. */
public final class TailRecursionElimination {
  private TailRecursionElimination() {}

  public static boolean run(Function function) {
    BasicBlock header = function.getEntryBlock();
    if (header == null || !header.getPredecessors().isEmpty() || hasAlloca(function)) return false;

    List<BasicBlock> tailBlocks = new ArrayList<>();
    List<Instruction> tailCalls = new ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      List<Instruction> instructions = block.getInstructions();
      if (instructions.size() < 2) continue;
      Instruction call = instructions.get(instructions.size() - 2);
      Instruction ret = instructions.get(instructions.size() - 1);
      if (call.getOpcode() == Instruction.Opcode.CALL
          && call.getCallee() == function
          && call.getNumOperands() == function.getNumArgs()
          && call.getNumUses() == 1
          && ret.getOpcode() == Instruction.Opcode.RET
          && ret.getNumOperands() == 1
          && ret.getOperand(0) == call) {
        tailBlocks.add(block);
        tailCalls.add(call);
      }
    }
    if (tailCalls.isEmpty()) return false;

    BasicBlock preheader = function.prependBlock("tail.entry");
    new IRBuilder(preheader).createBr(header);
    List<Instruction> argumentPhis = new ArrayList<>();
    for (Function.Argument argument : function.getArguments()) {
      Instruction phi = Instruction.createPhi(argument.getType());
      header.addInstructionToFront(phi);
      argument.replaceAllUsesWith(phi);
      phi.addOperand(argument);
      phi.addOperand(preheader);
      argumentPhis.add(phi);
    }

    for (int candidate = 0; candidate < tailCalls.size(); candidate++) {
      Instruction call = tailCalls.get(candidate);
      BasicBlock block = tailBlocks.get(candidate);
      for (int argument = 0; argument < argumentPhis.size(); argument++) {
        argumentPhis.get(argument).addOperand(call.getOperand(argument));
        argumentPhis.get(argument).addOperand(block);
      }
      block.getTerminator().eraseFromParent();
      call.eraseFromParent();
      new IRBuilder(block).createBr(header);
    }
    return true;
  }

  private static boolean hasAlloca(Function function) {
    return function.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.ALLOCA);
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return TailRecursionElimination.run(function)
          ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
