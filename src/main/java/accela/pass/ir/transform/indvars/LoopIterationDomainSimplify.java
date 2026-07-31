package accela.pass.ir.transform.indvars;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.IdentityHashMap;
import java.util.Map;

/** Narrows canonical loop domains using affine guards controlled by an enclosing induction. */
final class LoopIterationDomainSimplify {
  private LoopIterationDomainSimplify() {}

  static boolean needsCanonicalization(Function function, FunctionAnalysisManager fam) {
    var loops = fam.getResult(LoopAnalysis.class, function).loops();
    for (LoopAnalysis.Loop inner : loops) {
      if (inner.latches().size() <= 1) continue;
      for (Instruction innerPhi : inner.header().getInstructions()) {
        if (innerPhi.getOpcode() != Instruction.Opcode.PHI) break;
        for (BasicBlock block : inner.blocks()) {
          Instruction branch = block.getTerminator();
          if (branch == null
              || branch.getOpcode() != Instruction.Opcode.CONDBR
              || !(branch.getOperand(0) instanceof Instruction compare)
              || compare.getOpcode() != Instruction.Opcode.ICMP) continue;
          Instruction outerPhi = guardedOuterPhi(compare, innerPhi);
          if (outerPhi != null && loops.stream().anyMatch(outer ->
              outer != inner
                  && outer.header() == outerPhi.getParent()
                  && outer.contains(inner.header()))) return true;
        }
      }
    }
    return false;
  }

  private static Instruction guardedOuterPhi(Instruction compare, Instruction innerPhi) {
    if ("slt".equals(compare.getPredicate()) && compare.getOperand(1) == innerPhi
        && compare.getOperand(0) instanceof Instruction outerPhi) {
      return outerPhi;
    }
    if ("sgt".equals(compare.getPredicate()) && compare.getOperand(0) == innerPhi
        && compare.getOperand(1) instanceof Instruction outerPhi) {
      return outerPhi;
    }
    return null;
  }

  static boolean run(Function function, FunctionAnalysisManager fam) {
    var inductions =
        fam.getResult(InductionVariableAnalysis.class, function).allInductions();
    DominatorTreeAnalysis.Result dominators =
        fam.getResult(DominatorTreeAnalysis.class, function);
    Map<Instruction, InductionVariableAnalysis.Induction> byPhi = new IdentityHashMap<>();
    for (var induction : inductions) byPhi.put(induction.phi(), induction);

    boolean changed = false;
    for (var inner : inductions) {
      Candidate candidate = Candidate.match(inner, byPhi, dominators);
      if (candidate == null) continue;
      narrow(candidate);
      changed = true;
    }
    return changed;
  }

  private static void narrow(Candidate candidate) {
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(candidate.inner().predecessor().getTerminator());
    Value outerNext =
        builder.createAdd(candidate.outer().phi(), Constant.intConst(1));

    // Materialize signed min(bound, outer + 1) without adding control-flow to the outer loop.
    Value takeOuter = builder.createICmp("slt", outerNext, candidate.bound());
    Value mask = builder.createSub(
        Constant.intConst(0), builder.createZExt(takeOuter, candidate.bound().getType()));
    Value difference = builder.createXor(outerNext, candidate.bound());
    Value selected = builder.createAnd(difference, mask);
    Value narrowedBound = builder.createXor(candidate.bound(), selected);

    candidate.headerCompare().setOperand(1, narrowedBound);
    candidate.guardCompare().replaceAllUsesWith(Constant.boolConst(false));
    if (candidate.guardCompare().getNumUses() == 0) {
      candidate.guardCompare().eraseFromParent();
    }
  }

  private record Candidate(
      InductionVariableAnalysis.Induction inner,
      InductionVariableAnalysis.Induction outer,
      Instruction headerCompare,
      Instruction guardCompare,
      Value bound) {
    static Candidate match(
        InductionVariableAnalysis.Induction inner,
        Map<Instruction, InductionVariableAnalysis.Induction> byPhi,
        DominatorTreeAnalysis.Result dominators) {
      if (!isZeroBasedUnitStep(inner)) return null;
      Instruction headerCompare = canonicalUpperBoundCompare(inner);
      if (headerCompare == null) return null;
      Value bound = headerCompare.getOperand(1);
      if (!availableAt(bound, inner.predecessor(), inner.loop(), dominators)) return null;

      for (BasicBlock block : inner.loop().blocks()) {
        Instruction branch = block.getTerminator();
        if (branch == null
            || branch.getOpcode() != Instruction.Opcode.CONDBR
            || !(branch.getOperand(0) instanceof Instruction guard)
            || guard.getOpcode() != Instruction.Opcode.ICMP) continue;
        Instruction outerPhi = guardedOuterPhi(guard, inner.phi());
        var outer = byPhi.get(outerPhi);
        if (outer == null
            || outer.loop() == inner.loop()
            || !outer.loop().contains(inner.loop().header())
            || !isZeroBasedUnitStep(outer)
            || canonicalUpperBoundCompare(outer) == null
            || !hasOnlyGuardedEffects(inner, branch, dominators)) continue;
        return new Candidate(inner, outer, headerCompare, guard, bound);
      }
      return null;
    }

    private static boolean hasOnlyGuardedEffects(
        InductionVariableAnalysis.Induction inner,
        Instruction guardBranch,
        DominatorTreeAnalysis.Result dominators) {
      BasicBlock work = (BasicBlock) guardBranch.getOperand(2);
      for (BasicBlock block : inner.loop().blocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() == Instruction.Opcode.PHI
              && instruction.getParent() == inner.loop().header()
              && instruction != inner.phi()) return false;
          if (isObservable(instruction) && !dominators.dominates(work, block)) return false;
          if (instruction != inner.phi()
              && instruction.getUses().stream().anyMatch(
                  use -> !inner.loop().contains(use.getUser().getParent()))) return false;
        }
      }
      return true;
    }

    private static boolean isObservable(Instruction instruction) {
      return instruction.getOpcode() == Instruction.Opcode.LOAD
          || instruction.getOpcode() == Instruction.Opcode.STORE
          || instruction.getOpcode() == Instruction.Opcode.CALL
          || instruction.getOpcode() == Instruction.Opcode.RET;
    }

    private static Instruction guardedOuterPhi(Instruction compare, Instruction innerPhi) {
      return LoopIterationDomainSimplify.guardedOuterPhi(compare, innerPhi);
    }

    private static Instruction canonicalUpperBoundCompare(
        InductionVariableAnalysis.Induction induction) {
      Instruction branch = induction.loop().header().getTerminator();
      if (branch == null
          || branch.getOpcode() != Instruction.Opcode.CONDBR
          || !(branch.getOperand(0) instanceof Instruction compare)
          || compare.getOpcode() != Instruction.Opcode.ICMP
          || !"slt".equals(compare.getPredicate())
          || compare.getOperand(0) != induction.phi()
          || !induction.loop().contains((BasicBlock) branch.getOperand(1))
          || induction.loop().contains((BasicBlock) branch.getOperand(2))) return null;
      return compare;
    }

    private static boolean isZeroBasedUnitStep(
        InductionVariableAnalysis.Induction induction) {
      return induction.step() == 1
          && induction.start() instanceof Constant.Int start
          && start.value == 0;
    }

    private static boolean availableAt(
        Value value,
        BasicBlock insertionBlock,
        LoopAnalysis.Loop loop,
        DominatorTreeAnalysis.Result dominators) {
      if (!(value instanceof Instruction instruction)) return true;
      if (instruction.getParent() == insertionBlock) return true;
      return instruction.getParent() != null
          && !loop.contains(instruction.getParent())
          && dominators.dominates(instruction.getParent(), insertionBlock);
    }
  }
}
