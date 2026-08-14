package accela.pass.ir.transform.sroa;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.ConstantFolding;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;

/** Bounded local-array scalarization plus immutable constant aggregate folding. */
public final class ArrayObjectPromotion {
  private static final int MAX_ELEMENTS = 64;

  private ArrayObjectPromotion() {}

  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    public Pass(PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = instrumentation;
      this.descriptor = descriptor;
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      PassDecisionEmitter decision = instrumentation.decisionEmitter(
          descriptor, occurrence, "function", "@" + function.getName());
      decision.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      boolean changed = foldImmutableGlobals(function) | ScalarizeArrayAllocas.runOnFunction(
          function, MAX_ELEMENTS);
      if (changed) {
        decision.applied(DecisionReasonCode.APPLIED_CANONICALIZATION);
        return PreservedAnalyses.none();
      }
      decision.rejectedLegality("candidate.array-object-promotion.constant-index");
      return PreservedAnalyses.all();
    }

    private static boolean foldImmutableGlobals(Function function) {
      boolean changed = false;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction load : new ArrayList<>(block.getInstructions())) {
          if (load.getOpcode() != Instruction.Opcode.LOAD
              || !(load.getOperand(0) instanceof Instruction gep)
              || !(accela.pass.ir.analysis.alias.PointerProvenance.root(gep)
                  instanceof GlobalVariable global)
              || !global.isConstant()
              || global.getInitializer() == null) continue;
          Integer leaf = ConstantFolding.constantArrayIndex(global, gep);
          if (leaf == null) continue;
          Constant value = ConstantFolding.initializerAt(global, leaf);
          if (value == null || value.getType() != load.getType()) continue;
          load.replaceAllUsesWith(value);
          load.eraseFromParent();
          changed = true;
        }
      }
      return changed;
    }
  }
}
