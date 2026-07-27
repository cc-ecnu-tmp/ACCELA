package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayList;
import java.util.List;

/** Replaces direct self calls in tail position with jumps to a loop header. */
public final class TailRecursionElimination {
  private TailRecursionElimination() {}

  static boolean runOnFunction(Function function) {
    BasicBlock header = function.getEntryBlock();
    if (header == null || !header.getPredecessors().isEmpty() || hasAlloca(function)) return false;

    List<TailSite> sites = findTailSites(function);
    if (sites.isEmpty()) return false;

    BasicBlock entry = function.prependBlock("tailrecurse.entry");
    new IRBuilder(entry).createBr(header);

    List<Instruction> argumentPhis = new ArrayList<>();
    for (Function.Argument argument : function.getArguments()) {
      Instruction phi = Instruction.createPhi(argument.getType());
      phi.setName(argument.getName() + ".tr");
      header.addInstructionToFront(phi);
      argument.replaceAllUsesWith(phi);
      phi.addOperand(argument);
      phi.addOperand(entry);
      argumentPhis.add(phi);
    }

    for (TailSite site : sites) {
      for (int index = 0; index < argumentPhis.size(); index++) {
        argumentPhis.get(index).addOperand(site.call().getOperand(index));
        argumentPhis.get(index).addOperand(site.block());
      }
      IRBuilder builder = new IRBuilder();
      builder.setInsertPointBefore(site.ret());
      builder.createBr(header);
      site.ret().eraseFromParent();
      site.call().eraseFromParent();
    }
    return true;
  }

  private static List<TailSite> findTailSites(Function function) {
    List<TailSite> sites = new ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      List<Instruction> instructions = block.getInstructions();
      if (instructions.size() < 2) continue;
      Instruction call = instructions.get(instructions.size() - 2);
      Instruction ret = instructions.getLast();
      if (isTailSelfCall(function, call, ret)) sites.add(new TailSite(block, call, ret));
    }
    return sites;
  }

  private static boolean isTailSelfCall(
      Function function, Instruction call, Instruction ret) {
    if (call.getOpcode() != Instruction.Opcode.CALL
        || call.getCallee() != function
        || call.getNumOperands() != function.getNumArgs()
        || ret.getOpcode() != Instruction.Opcode.RET) {
      return false;
    }
    if (function.getReturnType() == accela.ir.Type.VOID) {
      return call.getType() == accela.ir.Type.VOID && ret.getNumOperands() == 0;
    }
    return ret.getNumOperands() == 1
        && ret.getOperand(0) == call
        && call.getNumUses() == 1;
  }

  private static boolean hasAlloca(Function function) {
    return function.getBlocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.ALLOCA);
  }

  private record TailSite(BasicBlock block, Instruction call, Instruction ret) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
