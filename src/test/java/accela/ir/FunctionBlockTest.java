package accela.ir;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;

import org.junit.jupiter.api.Test;

final class FunctionBlockTest {
  @Test
  void prependsNewEntryBlocks() {
    Function function = new Function("f", Type.VOID);
    BasicBlock oldEntry = function.addBlock("body");

    BasicBlock newEntry = function.prependBlock("entry");

    assertSame(newEntry, function.getEntryBlock());
    assertSame(function, newEntry.getParent());
    assertEquals(java.util.List.of(newEntry, oldEntry), function.getBlocks());
  }
}
