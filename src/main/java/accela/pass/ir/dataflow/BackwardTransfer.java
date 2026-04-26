package accela.pass.ir.dataflow;

import accela.ir.Instruction;

public interface BackwardTransfer<T> {
  T transferInstruction(Instruction inst, T out);
}
