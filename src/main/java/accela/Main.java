package accela;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import java.util.concurrent.Callable;

import picocli.CommandLine;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;

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

@Command(
    name = "accela",
    mixinStandardHelpOptions = true,
    version = "0.1.0",
    description = "ACCELA compiler",
    subcommands = {
        Main.BuildCmd.class,
        Main.RunCmd.class,
        Main.IrCmd.class
    }
)
public class Main implements Callable<Integer> {

  @Parameters(index = "0", description = "The source file", arity = "0..1")
  private String fileName;

  public static void main(String[] args) {
    int exitCode = new CommandLine(new Main()).execute(args);
    System.exit(exitCode);
  }

  @Override
  public Integer call() throws Exception {
    if (fileName != null) {
      String source = new String(Files.readAllBytes(Paths.get(fileName)));
      Lexer lexer = new Lexer(source, fileName);
      List<Token> tokens = lexer.tokenize();
      for (Token token : tokens) {
        System.out.println(token);
      }
      return 0;
    }
    CommandLine.usage(this, System.out);
    return 0;
  }

  private static Node buildFrontend(String fileName) throws Exception {
    String source = new String(Files.readAllBytes(Paths.get(fileName)));
    Lexer lexer = new Lexer(source, fileName);
    List<Token> tokens = lexer.tokenize();
    Parser parser = new Parser(tokens);
    Node unit = parser.parse();
    new Sema().analyze(unit);
    return unit;
  }

  private static accela.ir.Module buildRawIR(Node unit) {
    accela.ir.Module module = new AST2IR().convert(unit);
    IRVerifier.verifyModule(module);
    return module;
  }

  private static accela.ir.Module buildOptimizedIR(Node unit, boolean printPassStats, boolean sroaOnly) {
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

  private static void runBuild(String fileName, boolean printAst) throws Exception {
    Node unit = buildFrontend(fileName);
    if (printAst) {
      new AstDumper(System.out).dump(unit);
    } else {
      System.out.print(new BackendCompiler().compileToAssembly(buildOptimizedIR(unit, false, false)));
    }
  }

  private static void runInterpreter(String fileName) throws Exception {
    Node unit = buildFrontend(fileName);
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

  private static void runIr(String fileName, boolean raw, boolean sroa, boolean printPassStats)
      throws Exception {
    Node unit = buildFrontend(fileName);
    if (raw) {
      new IRPrinter(System.out).print(buildRawIR(unit));
    } else if (sroa) {
      new IRPrinter(System.out).print(buildOptimizedIR(unit, printPassStats, true));
    } else {
      new IRPrinter(System.out).print(buildOptimizedIR(unit, printPassStats, false));
    }
  }

  @Command(name = "build", description = "Build the source file and emit AST or assembly")
  static class BuildCmd implements Callable<Integer> {
    @Option(names = "--ast", description = "Print the AST")
    boolean printAst;

    @Option(names = {"-S", "--asm"}, description = "Emit assembly (default)")
    boolean emitAsm;

    @Parameters(index = "0", description = "The source file")
    String fileName;

    @Override
    public Integer call() throws Exception {
      runBuild(fileName, printAst);
      return 0;
    }
  }

  @Command(name = "run", description = "Interpret the source file")
  static class RunCmd implements Callable<Integer> {
    @Parameters(index = "0", description = "The source file")
    String fileName;

    @Override
    public Integer call() throws Exception {
      runInterpreter(fileName);
      return 0;
    }
  }

  @Command(name = "ir", description = "Emit LLVM-like IR")
  static class IrCmd implements Callable<Integer> {
    @Option(names = "--raw", description = "Emit raw IR")
    boolean raw;

    @Option(names = "--sroa", description = "Emit SROA IR")
    boolean sroa;

    @Option(names = "--pass-stats", description = "Print pass statistics")
    boolean printPassStats;

    @Parameters(index = "0", description = "The source file")
    String fileName;

    @Override
    public Integer call() throws Exception {
      runIr(fileName, raw, sroa, printPassStats);
      return 0;
    }
  }
}
