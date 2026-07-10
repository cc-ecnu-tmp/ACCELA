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

  @Test
  void insertsBlocksAfterExistingBlocks() {
    Function function = new Function("f", Type.VOID);
    BasicBlock first = function.addBlock("first");
    BasicBlock last = function.addBlock("last");

    BasicBlock middle = function.insertBlockAfter(first, "middle");

    assertSame(function, middle.getParent());
    assertEquals(java.util.List.of(first, middle, last), function.getBlocks());
  }
}
