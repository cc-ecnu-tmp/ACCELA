package accela.pass.ir.transform.indvars;

import accela.ir.Constant;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.InductionVariableAnalysis;

/** Replaces a rotated integer exit test with an aligned pointer end test. */
final class PointerLFTR {
  private PointerLFTR() {}

  static boolean rewrite(InductionVariableAnalysis.Induction induction) {
    RotatedExit exit = RotatedExit.match(induction);
    PointerInduction pointer = PointerInduction.find(induction);
    if (exit == null || pointer == null) return false;

    IRBuilder preheaderBuilder = new IRBuilder();
    preheaderBuilder.setInsertPointBefore(induction.predecessor().getTerminator());
    Value distance;
    if (exit.bound() instanceof Constant.Int bound) {
      distance = Constant.int64Const((int) bound.value - exit.start());
    } else {
      Value adjusted = exit.bound();
      if (exit.start() != 0) {
        adjusted =
            preheaderBuilder.createSub(adjusted, Constant.intConst(exit.start()));
      }
      distance = preheaderBuilder.createSExt(adjusted, Type.I64);
    }
    Instruction end = preheaderBuilder.createGEP(
        pointer.next().getGepSourceType(),
        pointer.initial(),
        new Value[] {distance},
        false);

    IRBuilder latchBuilder = new IRBuilder();
    latchBuilder.setInsertPointBefore(exit.branch());
    exit.branch().setOperand(
        0, latchBuilder.createICmp("ne", pointer.next(), end));
    if (!exit.compare().hasUses()) exit.compare().eraseFromParent();
    return true;
  }
}
