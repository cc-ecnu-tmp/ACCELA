package accela.pass;

import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPassManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.ModuleToFunctionPassAdaptor;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.PostDominatorTreeAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.ADCE;
import accela.pass.ir.transform.GlobalConstantPropagation;
import accela.pass.ir.transform.InstSimplify;
import accela.pass.ir.transform.Mem2Reg;
import accela.pass.ir.transform.SCCP;
import accela.pass.ir.transform.SimplifyCFG;
import accela.pass.ir.transform.SROA;
import accela.pass.ir.transform.EarlyCSE;

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
    fam.registerPass(PostDominatorTreeAnalysis.class, new PostDominatorTreeAnalysis());
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
    if (enableAdce) {
      fpm.addPass(new ADCE.Pass());
      if (enableSimplifyCfg) {
        fpm.addPass(new SimplifyCFG.Pass());
      }
    }

    ModulePassManager mpm = new ModulePassManager(instrumentation);
    mpm.addPass(new GlobalConstantPropagation.Pass());
    mpm.addPass(new ModuleToFunctionPassAdaptor(fpm));
    return mpm;
  }

  public ModulePassManager buildIRO0Pipeline() {
    return buildIRO0Pipeline(PassInstrumentation.noop());
  }
}
