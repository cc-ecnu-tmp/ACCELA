import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import parse.Lexer;
import parse.Lexer.Token;
import parse.Parser;
import parse.Sema;

// TODO: we need a better way of handling cmd args
public class Main {

  public static void main(String[] args) {
    if (args.length == 0) {
      System.err.println("Usage: java Main [--ast | --interpret] <source file>");
      System.exit(1);
    }

    boolean printAst = false;
    boolean interpret = false;
    String fileName;

    if (args[0].equals("--ast")) {
      printAst = true;
      fileName = args[1];
    } else if (args[0].equals("--interpret")) {
      interpret = true;
      fileName = args[1];
    } else {
      fileName = args[0];
    }

    try {
      String source = new String(Files.readAllBytes(Paths.get(fileName)));
      Lexer lexer = new Lexer(source, fileName);
      List<Token> tokens = lexer.tokenize();

      if (printAst || interpret) {
        Parser parser = new Parser(tokens);
        ast.Node unit = parser.parse();
        new Sema().analyze(unit);
        if (printAst) {
          new ast.AstDumper(System.out).dump(unit);
        } else {
          ast.Interpreter interpreter = new ast.Interpreter();
          interpreter.run(unit);
          Object exitCode = interpreter.getExitCode();
          if (exitCode != null) {
            System.out.println();
            if (exitCode instanceof Integer) {
              System.out.println((Integer) exitCode & 0xFF);
            } else {
              System.out.println(exitCode);
            }
          }
        }
      } else {
        for (Token token : tokens) {
          System.out.println(token);
        }
      }
    } catch (Exception e) {
      e.printStackTrace();
      System.exit(1);
    }
  }
}
