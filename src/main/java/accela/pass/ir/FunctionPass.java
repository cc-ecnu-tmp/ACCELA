package accela.pass.ir;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;

/** A transform pass that runs on one IR function. */
public interface FunctionPass {
  PreservedAnalyses run(Function function, FunctionAnalysisManager fam);
}
