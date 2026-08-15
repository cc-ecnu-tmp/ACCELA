package accela.pass;

import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.PostDominatorTreeAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
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
import accela.pass.ir.transform.SROA;
import accela.pass.ir.transform.StrengthReduction;
import accela.pass.ir.transform.TailRecursionElimination;
import accela.pass.ir.transform.gvn.GVN;
import accela.pass.ir.transform.indvars.IndVarSimplify;
import accela.pass.ir.transform.inliner.Inliner;
import accela.pass.ir.transform.loop.interchange.LoopInterchange;
import accela.pass.ir.transform.loop.load.LoopLoadElimination;
import accela.pass.ir.transform.loop.rotate.LoopRotate;
import accela.pass.ir.transform.loop.strength.LoopStrengthReduce;
import accela.pass.ir.transform.loop.unroll.LoopUnroll;
import accela.pass.ir.transform.loop.unroll.LoopUnrollAndJam;
import accela.pass.ir.transform.recurrence.RankedRecurrenceTabulation;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;
import accela.pass.ir.verify.IRVerifier;
import java.util.List;

/** Executes one registered IR occurrence on an isolated R2 snapshot. */
public final class R2IRPassExecutor {
  public boolean apply(R2PassOccurrence occurrence, accela.ir.Module module) {
    if (occurrence.stage() != PassDescriptor.Stage.IR) {
      throw new IllegalArgumentException("R2 IR executor received " + occurrence.id());
    }
    boolean changed = switch (occurrence.scope()) {
      case FUNCTION -> runFunctions(module, functionPass(occurrence.familyId()));
      case MODULE -> runModule(module, modulePass(occurrence.familyId()));
    };
    IRVerifier.verifyModule(module);
    return changed;
  }

  private static boolean runFunctions(accela.ir.Module module, FunctionPass pass) {
    FunctionAnalysisManager analyses = functionAnalyses();
    boolean changed = false;
    for (accela.ir.Function function : List.copyOf(module.getFunctions())) {
      PreservedAnalyses preserved = pass.run(function, analyses);
      analyses.invalidate(function, preserved);
      changed |= !preserved.preservesAll();
    }
    return changed;
  }

  private static boolean runModule(accela.ir.Module module, ModulePass pass) {
    ModuleAnalysisManager mam = new ModuleAnalysisManager();
    FunctionAnalysisManager fam = functionAnalyses();
    PreservedAnalyses preserved = pass.run(module, mam, fam);
    mam.invalidate(module, preserved);
    return !preserved.preservesAll();
  }

  private static FunctionAnalysisManager functionAnalyses() {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(PostDominatorTreeAnalysis.class, new PostDominatorTreeAnalysis());
    fam.registerPass(ScalarEvolutionAnalysis.class, new ScalarEvolutionAnalysis());
    return fam;
  }

  private static FunctionPass functionPass(String family) {
    return switch (family) {
      case R2PassRegistry.IR_SIMPLIFY_CFG -> new SimplifyCFG.Pass();
      case R2PassRegistry.IR_SROA -> new SROA.Pass();
      case R2PassRegistry.IR_MEM2REG -> new Mem2Reg.Pass();
      case R2PassRegistry.IR_EARLY_CSE -> new EarlyCSE.Pass();
      case R2PassRegistry.IR_SCCP -> new SCCP.Pass();
      case R2PassRegistry.IR_INST_SIMPLIFY -> new InstSimplify.Pass();
      case R2PassRegistry.IR_INST_COMBINE -> new InstCombine.Pass();
      case R2PassRegistry.IR_ADCE -> new ADCE.Pass();
      case R2PassRegistry.IR_TAIL_RECURSION -> new TailRecursionElimination.Pass();
      case R2PassRegistry.IR_INDVAR_DOMAIN -> new IndVarSimplify.DomainPass();
      case R2PassRegistry.IR_AFFINE_SUMMARY -> new AffineLoopSummarization.Pass();
      case R2PassRegistry.IR_REDUCTION_PUSHDOWN -> new ReductionPushdown.Pass();
      case R2PassRegistry.IR_LOOP_INTERCHANGE -> new LoopInterchange.Pass();
      case R2PassRegistry.IR_LOOP_ROTATE -> new LoopRotate.Pass();
      case R2PassRegistry.IR_LICM -> new LICM.Pass();
      case R2PassRegistry.IR_UNROLL_AND_JAM -> new LoopUnrollAndJam.Pass();
      case R2PassRegistry.IR_UNROLL -> new LoopUnroll.Pass();
      case R2PassRegistry.IR_INDVAR_SIMPLIFY -> new IndVarSimplify.Pass();
      case R2PassRegistry.IR_GVN -> new GVN.Pass();
      case R2PassRegistry.IR_LOOP_STRENGTH -> new LoopStrengthReduce.Pass();
      case R2PassRegistry.IR_LOOP_LOAD_ROTATE -> new LoopRotate.LoadEliminationPass();
      case R2PassRegistry.IR_LOOP_LOAD_ELIMINATION -> new LoopLoadElimination.Pass();
      case R2PassRegistry.IR_POINTER_LFTR -> new IndVarSimplify.LFTRPass();
      case R2PassRegistry.IR_STRENGTH -> new StrengthReduction.Pass();
      default -> throw new IllegalArgumentException("no R2 function factory for " + family);
    };
  }

  private static ModulePass modulePass(String family) {
    return switch (family) {
      case R2PassRegistry.IR_DEAD_STORE_ELIMINATION -> new DeadStoreElimination.Pass();
      case R2PassRegistry.IR_GLOBAL_DCE -> new ADCE.GlobalPass();
      case R2PassRegistry.IR_GLOBAL_OPT -> new GlobalOpt.Pass();
      case R2PassRegistry.IR_GLOBAL_SROA -> new SROA.GlobalPass();
      case R2PassRegistry.IR_IPSCCP -> new IPSCCP.Pass();
      case R2PassRegistry.IR_RRT -> new RankedRecurrenceTabulation.Pass();
      case R2PassRegistry.IR_INLINER -> new Inliner.Pass();
      default -> throw new IllegalArgumentException("no R2 module factory for " + family);
    };
  }
}
