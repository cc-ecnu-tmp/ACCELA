package accela;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import accela.parse.Lexer;
import accela.parse.Lexer.Token;
import accela.parse.Parser;
import accela.parse.Sema;
import accela.ast.*;
import accela.ir.*;

// TODO: we need a better way of handling cmd args
public class Main {

  public static void main(String[] args) {
    if (args.length == 0) {
      System.err.println("Usage: java Main [--ast | --interpret | --ir] <source file>");
      System.exit(1);
    }

    boolean printAst = false;
    boolean interpret = false;
    boolean emitIR = false;
    String fileName;

    if (args[0].equals("--ast")) {
      printAst = true;
      fileName = args[1];
    } else if (args[0].equals("--interpret")) {
      interpret = true;
      fileName = args[1];
    } else if (args[0].equals("--ir")) {
      emitIR = true;
      fileName = args[1];
    } else {
      fileName = args[0];
    }

    try {
      String source = new String(Files.readAllBytes(Paths.get(fileName)));
      Lexer lexer = new Lexer(source, fileName);
      List<Token> tokens = lexer.tokenize();

      if (printAst || interpret || emitIR) {
        Parser parser = new Parser(tokens);
        Node unit = parser.parse();
        new Sema().analyze(unit);
        if (printAst) {
          new AstDumper(System.out).dump(unit);
        } else if (emitIR) {
          accela.ir.Module module = new AST2IR().convert(unit);
          Mem2Reg.run(module);
          new IRPrinter(System.out).print(module);
        } else {
          Interpreter interpreter = new Interpreter();
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
