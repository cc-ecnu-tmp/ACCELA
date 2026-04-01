package accela.pass.ir;

/** An analysis that computes a cached result for one IR module. */
public interface ModuleAnalysis<ResultT> {
  ResultT run(accela.ir.Module module, ModuleAnalysisManager mam);
}
