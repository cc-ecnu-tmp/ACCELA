package accela.pass.ir;

import accela.ir.Function;

/** An analysis that computes a cached result for one IR function. */
public interface FunctionAnalysis<ResultT> {
  ResultT run(Function function, FunctionAnalysisManager fam);
}
