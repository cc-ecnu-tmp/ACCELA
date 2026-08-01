package accela.pass;

import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPassManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.ModuleToFunctionPassAdaptor;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.PostDominatorTreeAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.ADCE;
import accela.pass.ir.transform.AffineLoopSummarization;
import accela.pass.ir.transform.DeadStoreElimination;
import accela.pass.ir.transform.EarlyCSE;
import accela.pass.ir.transform.GlobalOpt;
import accela.pass.ir.transform.IPSCCP;
import accela.pass.ir.transform.InstCombine;
import accela.pass.ir.transform.InstSimplify;
import accela.pass.ir.transform.LICM;
import accela.pass.ir.transform.Mem2Reg;
import accela.pass.ir.transform.ReductionPushdown;
import accela.pass.ir.transform.SCCP;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;
import accela.pass.ir.transform.SROA;
import accela.pass.ir.transform.StrengthReduction;
import accela.pass.ir.transform.TailRecursionElimination;
import accela.pass.ir.transform.gvn.GVN;
import accela.pass.ir.transform.indvars.IndVarSimplify;
import accela.pass.ir.transform.inliner.Inliner;
import accela.pass.ir.transform.loop.interchange.LoopInterchange;
import accela.pass.ir.transform.loop.load.LoopLoadElimination;
import accela.pass.ir.transform.loop.strength.LoopStrengthReduce;
import accela.pass.ir.transform.loop.rotate.LoopRotate;
import accela.pass.ir.transform.loop.unroll.LoopUnroll;
import accela.pass.ir.transform.loop.unroll.LoopUnrollAndJam;
import accela.pass.ir.transform.recurrence.RankedRecurrenceTabulation;

/**
 * Builds the project's default pass pipelines.
 */
public final class PassBuilder {
  private static boolean isSimplifyCfgEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_SIMPLIFYCFG");
    return disable == null
        || disable.isEmpty()
        || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isSroaEnabled() {
    // TODO: Remove me once SROA testing is finished
    String disable = System.getenv("ACCELA_DISABLE_SROA");
    return disable == null || disable.isEmpty() || disable.equals("0") || disable.equalsIgnoreCase("false");
  }

  private static boolean isMem2RegEnabled() {
    // TODO: Remove me once SROA testing is finished      
    String disable = System.getenv("ACCELA_DISABLE_MEM2REG");
    return disable == null || disable.isEmpty() || disable.equals("0") || disable.equalsIgnoreCase("false");
  }

  private static boolean isAdceEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_ADCE");
    return disable == null || disable.isEmpty() || disable.equals("0") || disable.equalsIgnoreCase("false");
  }

  private static boolean isSccpEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_SCCP");
    return disable == null || disable.isEmpty() || disable.equals("0") || disable.equalsIgnoreCase("false");
  }

  private static boolean isInstSimplifyEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_INSTSIMPLIFY");
    return disable == null || disable.isEmpty() || disable.equals("0") || disable.equalsIgnoreCase("false");
  }

  private static boolean isEarlyCseEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_EARLYCSE");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isInlinerEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_INLINER");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isTailCallElimEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_TAILCALLELIM");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isLicmEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_LICM");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isLoopRotateEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_LOOP_ROTATE");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isLoopInterchangeEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_LOOP_INTERCHANGE");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isLoopStrengthReduceEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_LOOP_STRENGTH_REDUCE");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isLoopUnrollEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_LOOP_UNROLL");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isLoopUnrollAndJamEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_LOOP_UNROLL_AND_JAM");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isIndVarSimplifyEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_INDVAR_SIMPLIFY");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isAffineLoopSummarizationEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_AFFINE_LOOP_SUMMARIZATION");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  private static boolean isRankedRecurrenceTabulationEnabled() {
    String disable = System.getenv("ACCELA_DISABLE_RRT");
    return disable == null || disable.isEmpty() || disable.equals("0")
        || disable.equalsIgnoreCase("false");
  }

  /** Creates pass instrumentation with always-on verification and optional reporting. */
  public PassInstrumentation buildIRInstrumentation(boolean printReports) {
    return PassInstrumentation.enabled(printReports);
  }

  /** Creates a fresh module analysis manager. */
  public ModuleAnalysisManager buildModuleAnalysisManager() {
    return new ModuleAnalysisManager();
  }

  /** Creates a fresh function analysis manager. */
  public FunctionAnalysisManager buildFunctionAnalysisManager() {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(PostDominatorTreeAnalysis.class, new PostDominatorTreeAnalysis());
    fam.registerPass(ScalarEvolutionAnalysis.class, new ScalarEvolutionAnalysis());
    return fam;
  }

  /**
   * Builds the current default IR pipeline.
   *
   * <p>The current pipeline first scalarizes analyzable array allocas with SROA and then promotes
   * the resulting scalar slots with mem2reg.
   */
  public ModulePassManager buildIRO0Pipeline(PassInstrumentation instrumentation) {
    return buildIRO0Pipeline(
        instrumentation,
        isSimplifyCfgEnabled(),
        isSroaEnabled(),
        isMem2RegEnabled(),
        isAdceEnabled());
  }

  public ModulePassManager buildIRO0Pipeline(
      PassInstrumentation instrumentation, boolean enableSroa, boolean enableMem2Reg) {
    return buildIRO0Pipeline(
        instrumentation, isSimplifyCfgEnabled(), enableSroa, enableMem2Reg, isAdceEnabled());
  }

  public ModulePassManager buildIRO0Pipeline(
      PassInstrumentation instrumentation,
      boolean enableSimplifyCfg,
      boolean enableSroa,
      boolean enableMem2Reg,
      boolean enableAdce) {
    FunctionPassManager fpm = new FunctionPassManager(instrumentation);
    if (enableSimplifyCfg) {
      fpm.addPass(new SimplifyCFG.Pass());
    }
    // TODO: Remove me once SROA testing is finished
    if (enableSroa) {
      fpm.addPass(new SROA.Pass());
    }
    if (enableMem2Reg) {
      fpm.addPass(new Mem2Reg.Pass());
    }
    if (isEarlyCseEnabled()) {
      fpm.addPass(new EarlyCSE.Pass());
    }
    if (isSccpEnabled()) {
      fpm.addPass(new SCCP.Pass());
    }
    if (isEarlyCseEnabled()) {
      fpm.addPass(new EarlyCSE.Pass());
    }
    if (isInstSimplifyEnabled()) {
      fpm.addPass(new InstSimplify.Pass());
    }
    if (enableSroa) {
      fpm.addPass(new SROA.Pass());
    }
    if (isSccpEnabled()) {
      fpm.addPass(new SCCP.Pass());
    }
    if (isEarlyCseEnabled()) {
      fpm.addPass(new EarlyCSE.Pass());
    }
    if (isInstSimplifyEnabled()) {
      fpm.addPass(new InstSimplify.Pass());
    }
    fpm.addPass(new InstCombine.Pass());
    if (enableAdce) {
      fpm.addPass(new ADCE.Pass());
      if (enableSimplifyCfg) {
        fpm.addPass(new SimplifyCFG.Pass());
      }
    }

    ModulePassManager mpm = new ModulePassManager(instrumentation);
    FunctionPassManager globalMemoryFpm = new FunctionPassManager(instrumentation);
    globalMemoryFpm.addPass(new EarlyCSE.Pass());
    FunctionPassManager postIpsccpFpm = new FunctionPassManager(instrumentation);
    if (isIndVarSimplifyEnabled()) {
      postIpsccpFpm.addPass(new IndVarSimplify.DomainPass());
    }
    if (isAffineLoopSummarizationEnabled()) {
      postIpsccpFpm.addPass(new AffineLoopSummarization.Pass());
    }
    postIpsccpFpm.addPass(new ReductionPushdown.Pass());
    if (isLoopInterchangeEnabled()) {
      postIpsccpFpm.addPass(new LoopInterchange.Pass());
    }
    if (isLoopRotateEnabled()) {
      postIpsccpFpm.addPass(new LoopRotate.Pass());
    }
    if (isLicmEnabled()) {
      postIpsccpFpm.addPass(new LICM.Pass());
      if (isEarlyCseEnabled()) {
        postIpsccpFpm.addPass(new EarlyCSE.Pass());
      }
    }
    if (isLoopUnrollAndJamEnabled()) {
      postIpsccpFpm.addPass(new LoopUnrollAndJam.Pass());
    }
    if (isLoopUnrollEnabled()) {
      postIpsccpFpm.addPass(new LoopUnroll.Pass());
      postIpsccpFpm.addPass(new LoopUnroll.Pass());
    }
    if (isIndVarSimplifyEnabled()) {
      postIpsccpFpm.addPass(new IndVarSimplify.Pass());
      postIpsccpFpm.addPass(new InstSimplify.Pass());
      postIpsccpFpm.addPass(new SimplifyCFG.Pass());
    }
    postIpsccpFpm.addPass(new GVN.Pass());
    if (isLoopStrengthReduceEnabled()) {
      postIpsccpFpm.addPass(new LoopStrengthReduce.Pass());
      if (isLoopRotateEnabled()) {
        postIpsccpFpm.addPass(new LoopRotate.LoadEliminationPass());
        postIpsccpFpm.addPass(new LoopLoadElimination.Pass());
        if (isIndVarSimplifyEnabled()) {
          postIpsccpFpm.addPass(new IndVarSimplify.LFTRPass());
        }
      }
      if (isLicmEnabled()) {
        postIpsccpFpm.addPass(new LICM.Pass());
      }
    }
    postIpsccpFpm.addPass(new StrengthReduction.Pass());
    if (isLoopStrengthReduceEnabled() && enableAdce) {
      postIpsccpFpm.addPass(new ADCE.Pass());
    }
    FunctionPassManager preInlineFpm = new FunctionPassManager(instrumentation);
    preInlineFpm.addPass(new EarlyCSE.Pass());
    if (isTailCallElimEnabled()) {
      preInlineFpm.addPass(new TailRecursionElimination.Pass());
    }
    FunctionPassManager postInlineFpm = new FunctionPassManager(instrumentation);
    postInlineFpm.addPass(new SimplifyCFG.Pass());
    postInlineFpm.addPass(new EarlyCSE.Pass());
    postInlineFpm.addPass(new SCCP.Pass());
    postInlineFpm.addPass(new InstSimplify.Pass());
    postInlineFpm.addPass(new InstCombine.Pass());
    postInlineFpm.addPass(new ADCE.Pass());
    postInlineFpm.addPass(new SimplifyCFG.Pass());
    mpm.addPass(new ModuleToFunctionPassAdaptor(fpm));
    mpm.addPass(new ModuleToFunctionPassAdaptor(globalMemoryFpm));
    mpm.addPass(new DeadStoreElimination.Pass());
    mpm.addPass(new ADCE.GlobalPass());
    mpm.addPass(new GlobalOpt.Pass());
    mpm.addPass(new SROA.GlobalPass());
    mpm.addPass(new IPSCCP.Pass());
    if (isRankedRecurrenceTabulationEnabled()) {
      mpm.addPass(new RankedRecurrenceTabulation.Pass());
    }
    mpm.addPass(new ModuleToFunctionPassAdaptor(preInlineFpm));
    if (isInlinerEnabled()) {
      mpm.addPass(new Inliner.Pass());
      mpm.addPass(new ModuleToFunctionPassAdaptor(postInlineFpm));
      mpm.addPass(new IPSCCP.Pass());
      mpm.addPass(new ADCE.GlobalPass());
      mpm.addPass(new GlobalOpt.Pass());
    }
    mpm.addPass(new ModuleToFunctionPassAdaptor(postIpsccpFpm));
    mpm.addPass(new ADCE.GlobalPass());
    return mpm;
  }

  public ModulePassManager buildIRO0Pipeline() {
    return buildIRO0Pipeline(PassInstrumentation.noop());
  }
}
