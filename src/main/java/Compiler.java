import accela.ast.Interpreter;
import accela.ast.Node;
import accela.Main;
import accela.backend.BackendCompiler;
import accela.backend.target.RISCVTargetOptions;
import accela.ir.AST2IR;
import accela.parse.Lexer;
import accela.parse.Parser;
import accela.parse.Sema;
import accela.pass.PassBuilder;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.verify.IRVerifier;

import java.io.PrintStream;
import java.nio.file.Files;
import java.nio.file.Paths;

public class Compiler {
  public static void main(String[] args) throws Exception {
    CompileArgument compileArgument = parseArguments(args);
    Node unit = parseSource(compileArgument.inputFilePath());

    if (compileArgument.outputFilePath() != null) {
      RISCVTargetOptions targetOptions =
          Main.targetOptions(compileArgument.simd(), compileArgument.minimumVLEN());
      String assembly =
          new BackendCompiler(targetOptions).compileToAssembly(buildOptimizedIR(unit));
      try (PrintStream stream = new PrintStream(compileArgument.outputFilePath())) {
        stream.print(assembly);
      }
    } else {
      Interpreter interpreter = new Interpreter();
      interpreter.run(unit);
      Object exitCode = interpreter.getExitCode();
      if (exitCode instanceof Number) {
        System.exit(((Number) exitCode).intValue());
      }
    }
  }

  private static Node parseSource(String inputFilePath) throws Exception {
    String source = new String(Files.readAllBytes(Paths.get(inputFilePath)));
    Lexer lexer = new Lexer(source, inputFilePath);
    Parser parser = new Parser(lexer.tokenize());
    Node unit = parser.parse();
    new Sema().analyze(unit);
    return unit;
  }

  private static accela.ir.Module buildOptimizedIR(Node unit) {
    accela.ir.Module module = new AST2IR().convert(unit);
    IRVerifier.verifyModule(module);

    PassBuilder passBuilder = new PassBuilder();
    ModuleAnalysisManager mam = passBuilder.buildModuleAnalysisManager();
    FunctionAnalysisManager fam = passBuilder.buildFunctionAnalysisManager();
    PassInstrumentation instrumentation = passBuilder.buildIRInstrumentation(false);
    ModulePassManager irPipeline = passBuilder.buildIRO0Pipeline(instrumentation);
    irPipeline.run(module, mam, fam);
    IRVerifier.verifyModule(module);
    return module;
  }

  static CompileArgument parseArguments(String[] args) {
    String inputFilePath = null;
    String outputFilePath = null;
    String simd = null;
    int minimumVLEN = RISCVTargetOptions.DEFAULT_VLEN;

    for (int i = 0; i < args.length; i++) {
      String arg = args[i];
      if (arg.isEmpty()) {
        continue;
      }
      if (arg.charAt(0) == '-') {
        if (arg.equals("-o")) {
          if (i + 1 >= args.length) {
            throw new IllegalArgumentException("`-o` requires an argument");
          }
          outputFilePath = args[++i];
        } else if (arg.equals("--simd")) {
          if (i + 1 >= args.length) throw new IllegalArgumentException("`--simd` requires an argument");
          simd = args[++i];
        } else if (arg.startsWith("--simd=")) {
          simd = arg.substring("--simd=".length());
        } else if (arg.equals("--riscv-vlen")) {
          if (i + 1 >= args.length)
            throw new IllegalArgumentException("`--riscv-vlen` requires an argument");
          minimumVLEN = Integer.parseInt(args[++i]);
        } else if (arg.startsWith("--riscv-vlen=")) {
          minimumVLEN = Integer.parseInt(arg.substring("--riscv-vlen=".length()));
        }
      } else {
        inputFilePath = arg;
      }
    }

    if (inputFilePath == null) {
      throw new IllegalArgumentException("no input file is provided");
    }
    return new CompileArgument(inputFilePath, outputFilePath, simd, minimumVLEN);
  }

  record CompileArgument(
      String inputFilePath, String outputFilePath, String simd, int minimumVLEN) {}
}
