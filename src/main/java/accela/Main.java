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
import accela.cli.*;

@Command(
    name = "java Main",
    mixinStandardHelpOptions = true,
    description = "Accela compiler command line interface."
)
public class Main implements Runnable {
  @Option(names = {"--ast"}, description = "Print AST")
  private boolean printAst = false;

  @Option(names = {"--interpret"}, description = "Run interpreter")
  private boolean interpret = false;

  @Option(names = {"--ir-raw"}, description = "Emit raw IR")
  private boolean emitRawIR = false;

  @Option(names = {"--ir-sroa"}, description = "Emit SROA optimized IR")
  private boolean emitSroaIR = false;

  @Option(names = {"--ir"}, description = "Emit optimized IR")
  private boolean emitIR = false;

  @Option(names = {"--asm"}, description = "Emit assembly")
  private boolean emitAsm = false;

  @Option(names = {"--pass-stats"}, description = "Print pass statistics")
  private boolean printPassStats = false;

  @Parameters(index = "0", description = "Source file to compile/run", required = true)
  private String fileName;

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

  @Override
  public void run() {
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
      throw new RuntimeException(e);
    }
  }

  public static void main(String[] args) {
    int exitCode = new CommandLine(new Main()).execute(args);
    System.exit(exitCode);
  }
}
