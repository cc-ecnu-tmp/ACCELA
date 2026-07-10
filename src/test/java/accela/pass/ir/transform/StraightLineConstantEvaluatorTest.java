package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import java.util.List;
import org.junit.jupiter.api.Test;

final class StraightLineConstantEvaluatorTest {
  @Test
  void evaluatesPureIntegerArithmetic() {
    Function function = new Function("pure", Type.INT);
    Value left = function.addArgument(Type.INT, "left");
    Value right = function.addArgument(Type.INT, "right");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value product = builder.createMul(left, right);
    builder.createRet(builder.createAdd(product, Constant.intConst(1)));

    Constant.Int result = StraightLineConstantEvaluator.evaluate(
        function, List.of(Constant.intConst(6), Constant.intConst(7)));

    assertEquals(43, result.value);
  }

  @Test
  void rejectsMemoryOperations() {
    Function function = new Function("memory", Type.INT);
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value storage = builder.createAlloca(Type.INT);
    builder.createRet(builder.createLoad(Type.INT, storage));

    assertNull(StraightLineConstantEvaluator.evaluate(function, List.of()));
  }
}
