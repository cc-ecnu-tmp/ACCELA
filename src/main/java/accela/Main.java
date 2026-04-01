package accela;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import accela.backend.BackendCompiler;
import accela.pass.PassBuilder;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.verify.IRVerifier;
import accela.parse.Lexer;
import accela.parse.Lexer.Token;
import accela.parse.Parser;
import accela.parse.Sema;
import accela.ast.*;
import accela.ir.*;

// TODO: we need a better way of handling cmd args
public class Main {
  private static accela.ir.Module buildRawIR(Node unit) {
    accela.ir.Module module = new AST2IR().convert(unit);
    IRVerifier.verifyModule(module);
    return module;
  }

  private static accela.ir.Module buildOptimizedIR(Node unit, boolean printPassStats) {
    return buildOptimizedIR(unit, printPassStats, false);
  }

  private static accela.ir.Module buildOptimizedIR(
      Node unit, boolean printPassStats, boolean sroaOnly) {
    accela.ir.Module module = buildRawIR(unit);

    PassBuilder passBuilder = new PassBuilder();
    ModuleAnalysisManager mam = passBuilder.buildModuleAnalysisManager();
    FunctionAnalysisManager fam = passBuilder.buildFunctionAnalysisManager();
    PassInstrumentation instrumentation = passBuilder.buildIRInstrumentation(printPassStats);
    ModulePassManager irPipeline =
        passBuilder.buildIRO0Pipeline(instrumentation, true, !sroaOnly);
    irPipeline.run(module, mam, fam);
    IRVerifier.verifyModule(module);

    return module;
  }

  public static void main(String[] args) {
    if (args.length == 0) {
      System.err.println(
          "Usage: java Main [--ast | --interpret | --ir-raw | --ir-sroa | --ir | --asm] [--pass-stats] <source file>");
      System.exit(1);
    }

    boolean printAst = false;
    boolean interpret = false;
    boolean emitRawIR = false;
    boolean emitSroaIR = false;
    boolean emitIR = false;
    boolean emitAsm = false;
    boolean printPassStats = false;
    String fileName;

    int argIndex = 0;
    while (argIndex < args.length - 1) {
      switch (args[argIndex]) {
        case "--ast":
          printAst = true;
          break;
        case "--interpret":
          interpret = true;
          break;
        case "--ir-raw":
          emitRawIR = true;
          break;
        case "--ir":
          emitIR = true;
          break;
        case "--ir-sroa":
          emitSroaIR = true;
          break;
        case "--asm":
          emitAsm = true;
          break;
        case "--pass-stats":
          printPassStats = true;
          break;
        default:
          argIndex = args.length - 1;
          continue;
      }
      argIndex++;
    }
    fileName = args[argIndex];

    try {
      String source = new String(Files.readAllBytes(Paths.get(fileName)));
      Lexer lexer = new Lexer(source, fileName);
      List<Token> tokens = lexer.tokenize();

      if (printAst || interpret || emitRawIR || emitSroaIR || emitIR || emitAsm) {
        Parser parser = new Parser(tokens);
        Node unit = parser.parse();
        new Sema().analyze(unit);
        if (printAst) {
          new AstDumper(System.out).dump(unit);
        } else if (emitRawIR) {
          new IRPrinter(System.out).print(buildRawIR(unit));
        } else if (emitSroaIR) {
          new IRPrinter(System.out).print(buildOptimizedIR(unit, printPassStats, true));
        } else if (emitIR) {
          new IRPrinter(System.out).print(buildOptimizedIR(unit, printPassStats));
        } else if (emitAsm) {
          System.out.print(new BackendCompiler().compileToAssembly(buildOptimizedIR(unit, printPassStats)));
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
