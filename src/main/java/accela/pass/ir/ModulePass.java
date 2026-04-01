package accela.pass.ir;

import accela.pass.PreservedAnalyses;

/** A transform pass that runs on one IR module. */
public interface ModulePass {
  PreservedAnalyses run(
      accela.ir.Module module,
      ModuleAnalysisManager mam,
      FunctionAnalysisManager fam);
}
