package accela.pass.ir.transform.loopstrengthreduce;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class AffineValueFormTest {
  @Test
  void computesDifferencesAcrossEquivalentLinearForms() {
    Function function = new Function("forms", Type.VOID);
    Value x = function.addArgument(Type.INT, "x");
    Value y = function.addArgument(Type.INT, "y");
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    Value left = builder.createAdd(
        builder.createMul(x, Constant.intConst(2)), y);
    Value right = builder.createAdd(
        builder.createAdd(x, x), builder.createAdd(y, Constant.intConst(4)));

    assertEquals(4L, AffineValueForm.difference(left, right));
    assertEquals(-4L, AffineValueForm.difference(right, left));
    assertNull(AffineValueForm.difference(x, y));
  }

  @Test
  void looksThroughExtensionsAndConstantOffsets() {
    Function function = new Function("offsets", Type.VOID);
    Value x = function.addArgument(Type.INT, "x");
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    Value left = builder.createSExt(builder.createSub(x, Constant.intConst(1)), Type.I64);
    Value right = builder.createSExt(builder.createAdd(x, Constant.intConst(1)), Type.I64);

    assertEquals(2L, AffineValueForm.difference(left, right));
  }
}
