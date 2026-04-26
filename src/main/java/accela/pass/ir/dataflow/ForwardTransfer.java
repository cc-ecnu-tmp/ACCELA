package accela.pass.ir.dataflow;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import java.util.Map;

public interface ForwardTransfer<T> {
  T transferInstruction(Instruction inst, T in);
  Map<BasicBlock, T> transferTerminator(Instruction term, T in);
}
