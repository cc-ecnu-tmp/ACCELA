package accela.pass.ir.transform.sccp;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.Set;
import org.junit.jupiter.api.Test;

final class SCCPTransferTest {
  @Test
  void waitsForUnknownBranchConditions() {
    Function function = new Function("branch", Type.INT);
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock left = function.addBlock("left");
    BasicBlock right = function.addBlock("right");
    Instruction branch = new IRBuilder(entry).createCondBr(condition, left, right);
    SCCP.SCCPTransfer transfer = new SCCP.SCCPTransfer();

    assertTrue(transfer.transferTerminator(branch, new SCCP.SCCPFact()).isEmpty());

    SCCP.SCCPFact overdefined =
        new SCCP.SCCPFact().with(condition, SCCP.ConstVal.TOP);
    assertEquals(
        Set.of(left, right), transfer.transferTerminator(branch, overdefined).keySet());
  }
}
