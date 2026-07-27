package accela.pass.ir.transform.loop.interchange;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.ir.analysis.InductionVariableAnalysis.Induction;
import java.util.ArrayList;
import java.util.List;

/**
 * Exchanges two canonical loop dimensions while keeping the existing CFG nesting.
 *
 * <p>The outer and inner induction PHIs retain their structural positions but exchange iteration
 * spaces. Payload operands exchange the semantic IVs, so the memory operations still compute the
 * same source-level subscripts under the new execution order.
 */
final class LoopInterchangeTransform {
  private LoopInterchangeTransform() {}

  static void apply(LoopInterchangeCandidate candidate) {
    var outer = candidate.outerInduction();
    var inner = candidate.innerInduction();
    swapStarts(outer, inner);

    candidate.outerCompare().setOperand(1, candidate.innerBound());
    candidate.innerCompare().setOperand(1, candidate.outerBound());
    String outerPredicate = candidate.outerCompare().getPredicate();
    candidate.outerCompare().setPredicate(candidate.innerCompare().getPredicate());
    candidate.innerCompare().setPredicate(outerPredicate);

    outer.next().setOperand(1, Constant.intConst(inner.step()));
    inner.next().setOperand(1, Constant.intConst(outer.step()));
    swapPayloadUses(candidate);
  }

  private static void swapStarts(Induction outer, Induction inner) {
    int outerStart = incomingIndex(outer.phi(), outer.predecessor());
    int innerStart = incomingIndex(inner.phi(), inner.predecessor());
    Value value = outer.phi().getOperand(outerStart);
    outer.phi().setOperand(outerStart, inner.phi().getOperand(innerStart));
    inner.phi().setOperand(innerStart, value);
  }

  private static void swapPayloadUses(LoopInterchangeCandidate candidate) {
    Instruction outer = candidate.outerInduction().phi();
    Instruction inner = candidate.innerInduction().phi();
    List<UseSite> outerUses =
        payloadUses(outer, candidate.innerBody(), candidate.outerInduction().next());
    List<UseSite> innerUses =
        payloadUses(inner, candidate.innerBody(), candidate.innerInduction().next());
    for (UseSite use : outerUses) use.instruction().setOperand(use.index(), inner);
    for (UseSite use : innerUses) use.instruction().setOperand(use.index(), outer);
  }

  private static List<UseSite> payloadUses(
      Instruction induction, BasicBlock body, Instruction next) {
    List<UseSite> uses = new ArrayList<>();
    for (Use use : induction.getUses()) {
      if (use.getUser().getParent() == body && use.getUser() != next) {
        uses.add(new UseSite(use.getUser(), use.getOperandIndex()));
      }
    }
    return uses;
  }

  private static int incomingIndex(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return index;
    }
    throw new IllegalStateException("induction has no incoming value from its predecessor");
  }

  private record UseSite(Instruction instruction, int index) {}
}
