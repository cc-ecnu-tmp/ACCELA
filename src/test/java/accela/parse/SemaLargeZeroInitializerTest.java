package accela.parse;

import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ast.Node;
import accela.ir.AST2IR;
import accela.ir.Constant;
import java.util.List;
import org.junit.jupiter.api.Test;

final class SemaLargeZeroInitializerTest {
  @Test
  void preservesLargeEmptyArrayInitializerSparsely() {
    String source = "int buffer[50000000] = {}; int main() { return 0; }";
    Node unit = new Parser(new Lexer(source, "large-zero.sy").tokenize()).parse();

    new Sema().analyze(unit);

    assertTrue(unit.kids.get(0).kids.get(0).kids.isEmpty());
    assertTrue(
        new AST2IR().convert(unit).getGlobals().get(0).getInitializer() instanceof Constant.Zero);
  }
}
