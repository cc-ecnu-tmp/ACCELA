package accela.pass.ir.transform.scan;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.Set;

/** Fully proved repeated-prefix or repeated-suffix reduction ready for one CFG rewrite. */
record PrefixScanCandidate(
    Kind kind,
    LoopAnalysis.Loop innerLoop,
    BasicBlock outerPreheader,
    BasicBlock outerHeader,
    BasicBlock innerHeader,
    BasicBlock innerBody,
    BasicBlock outerLatch,
    Instruction outerInduction,
    Value outerBound,
    Instruction innerInduction,
    Instruction reduction,
    Instruction reductionUpdate,
    Value reductionStart,
    Value term,
    Instruction outputStore,
    Value outputPointer,
    Set<Instruction> termInstructions,
    Set<Instruction> outputAddressInstructions) {

  enum Kind {
    FORWARD_PREFIX,
    REVERSE_SUFFIX
  }

  PrefixScanCandidate {
    termInstructions = Set.copyOf(termInstructions);
    outputAddressInstructions = Set.copyOf(outputAddressInstructions);
  }
}
