package accela.pass.ir.transform.sccp;

import accela.ir.Module;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;

/** Interprocedural sparse conditional constant propagation for whole SysY programs. */
public final class IPSCCP {
  private IPSCCP() {}

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return new IPSCCPSolver(module).solve()
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
