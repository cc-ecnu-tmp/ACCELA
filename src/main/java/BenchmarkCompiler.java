import accela.ast.Node;
import accela.backend.BackendCompiler;
import accela.backend.instrument.BackendPassInstrumentation;
import accela.benchmark.JsonlRemarkWriter;
import accela.ir.AST2IR;
import accela.parse.Lexer;
import accela.parse.Parser;
import accela.parse.Sema;
import accela.pass.PassBuilder;
import accela.pass.PassRegistry;
import accela.pass.PipelineProfile;
import accela.pass.candidate.Rv64WordPressureCandidate;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.verify.IRVerifier;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.io.IOException;
import java.util.List;
import java.util.HashSet;
import java.util.Objects;
import java.util.Set;

/** Evaluation-only compiler entry point with strict pass ablation and JSONL observations. */
public final class BenchmarkCompiler {
  private static final DevelopmentPipeline PRODUCTION_PIPELINE = new DevelopmentPipeline() {
    @Override
    public PassRegistry registry() {
      return PassRegistry.standard();
    }

    @Override
    public PassBuilder passBuilder(PassInstrumentation instrumentation) {
      return PassBuilder.withStandardCandidateImplementations(instrumentation);
    }

    @Override
    public BackendCompiler backendCompiler(
        PipelineProfile profile, BackendPassInstrumentation instrumentation) {
      return new BackendCompiler(
          profile,
          instrumentation,
          new accela.backend.BackendPipeline.CandidatePassProvider(
              java.util.Map.of(
                  Rv64WordPressureCandidate.ID,
                  Rv64WordPressureCandidate.factory(instrumentation))));
    }
  };

  private BenchmarkCompiler() {}

  public static void main(String[] args) throws Exception {
    run(args, PRODUCTION_PIPELINE);
  }

  /** Package-private explicit injection seam for full development-entry tests only. */
  static void runForTesting(String[] args, DevelopmentPipeline pipeline) throws Exception {
    run(args, Objects.requireNonNull(pipeline, "pipeline"));
  }

  private static void run(String[] args, DevelopmentPipeline pipeline) throws Exception {
    PassRegistry registry = Objects.requireNonNull(pipeline.registry(), "pipeline registry");
    if (args.length > 0 && "--export-registry".equals(args[0])) {
      Path output = parseRegistryExport(args);
      requireExistingParent(output, "registry output");
      rejectNonFileOutput(output, "registry output");
      registry.writeJson(output);
      return;
    }
    Arguments arguments = parseArguments(args);
    validatePaths(arguments);
    PipelineProfile profile = PipelineProfile.fromJson(arguments.profile(), registry);
    Node unit = parseSource(arguments.input());
    accela.ir.Module module = new AST2IR().convert(unit);
    IRVerifier.verifyModule(module);

    try (JsonlRemarkWriter remarks = new JsonlRemarkWriter(arguments.remarks());
        PassInstrumentation irInstrumentation = PassInstrumentation.observed(false, remarks);
        BackendPassInstrumentation backendInstrumentation =
            BackendPassInstrumentation.observed(remarks)) {
      PassBuilder passBuilder = Objects.requireNonNull(
          pipeline.passBuilder(irInstrumentation), "development IR pass builder");
      BackendCompiler backendCompiler = Objects.requireNonNull(
          pipeline.backendCompiler(profile, backendInstrumentation),
          "development backend compiler");
      ModuleAnalysisManager mam = passBuilder.buildModuleAnalysisManager();
      FunctionAnalysisManager fam = passBuilder.buildFunctionAnalysisManager();
      ModulePassManager irPipeline = passBuilder.buildIRO0Pipeline(irInstrumentation, profile);
      irPipeline.run(module, mam, fam);
      IRVerifier.verifyModule(module);
      String assembly = Objects.requireNonNull(
          backendCompiler.compileToAssembly(module), "development pipeline assembly");
      Files.writeString(arguments.output(), assembly, StandardCharsets.UTF_8);
    }
  }

  interface DevelopmentPipeline {
    PassRegistry registry();

    PassBuilder passBuilder(PassInstrumentation instrumentation);

    BackendCompiler backendCompiler(
        PipelineProfile profile, BackendPassInstrumentation instrumentation);
  }

  static Path parseRegistryExport(String[] args) {
    if (args.length != 2 || !"--export-registry".equals(args[0]) || args[1].isBlank()) {
      throw new IllegalArgumentException(
          "registry export requires exactly: --export-registry OUTPUT");
    }
    return Path.of(args[1]);
  }

  static Arguments parseArguments(String[] args) {
    Path input = null;
    Path output = null;
    Path profile = null;
    Path remarks = null;
    Set<String> seenOptions = new HashSet<>();
    for (int index = 0; index < args.length; index++) {
      String argument = args[index];
      if (argument.isBlank()) throw new IllegalArgumentException("arguments must not be blank");
      if (!argument.startsWith("-")) {
        if (input != null) throw new IllegalArgumentException("multiple input files are not supported");
        input = Path.of(argument);
        continue;
      }
      if (!Set.of("-o", "--profile", "--remarks").contains(argument)) {
        throw new IllegalArgumentException("unknown option '" + argument + "'");
      }
      if (!seenOptions.add(argument)) {
        throw new IllegalArgumentException("duplicate option '" + argument + "'");
      }
      if (++index >= args.length || args[index].isBlank()) {
        throw new IllegalArgumentException("option '" + argument + "' requires a value");
      }
      Path value = Path.of(args[index]);
      switch (argument) {
        case "-o" -> output = value;
        case "--profile" -> profile = value;
        case "--remarks" -> remarks = value;
        default -> throw new AssertionError(argument);
      }
    }
    if (input == null) throw new IllegalArgumentException("no input file is provided");
    if (output == null) throw new IllegalArgumentException("-o is required");
    if (profile == null) throw new IllegalArgumentException("--profile is required");
    if (remarks == null) throw new IllegalArgumentException("--remarks is required");
    return new Arguments(input, output, profile, remarks);
  }

  private static void validatePaths(Arguments arguments) throws IOException {
    if (!Files.isRegularFile(arguments.input())) {
      throw new IllegalArgumentException("input must be an existing regular file");
    }
    if (!Files.isRegularFile(arguments.profile())) {
      throw new IllegalArgumentException("profile must be an existing regular file");
    }
    requireExistingParent(arguments.output(), "output");
    requireExistingParent(arguments.remarks(), "remarks");
    List<Path> paths = List.of(arguments.input(), arguments.output(),
        arguments.profile(), arguments.remarks());
    for (int first = 0; first < paths.size(); first++) {
      for (int second = first + 1; second < paths.size(); second++) {
        if (samePath(paths.get(first), paths.get(second))) {
          throw new IllegalArgumentException(
              "input, output, profile, and remarks paths must be distinct");
        }
      }
    }
    rejectNonFileOutput(arguments.output(), "output");
    rejectNonFileOutput(arguments.remarks(), "remarks");
  }

  private static void requireExistingParent(Path path, String name) {
    Path absolute = normalized(path);
    Path parent = absolute.getParent();
    if (parent == null || !Files.isDirectory(parent)) {
      throw new IllegalArgumentException(name + " parent directory does not exist");
    }
  }

  private static Path normalized(Path path) {
    return path.toAbsolutePath().normalize();
  }

  private static boolean samePath(Path first, Path second) throws IOException {
    if (Files.exists(first) && Files.exists(second) && Files.isSameFile(first, second)) return true;
    return canonicalDestination(first).equals(canonicalDestination(second));
  }

  private static Path canonicalDestination(Path path) throws IOException {
    Path absolute = normalized(path);
    if (Files.exists(absolute)) return absolute.toRealPath();
    Path parent = absolute.getParent();
    if (parent == null) return absolute;
    return parent.toRealPath().resolve(absolute.getFileName()).normalize();
  }

  private static void rejectNonFileOutput(Path path, String name) {
    if (Files.exists(path) && !Files.isRegularFile(path)) {
      throw new IllegalArgumentException(name + " must be a regular file when it already exists");
    }
  }

  private static Node parseSource(Path input) throws Exception {
    String source = Files.readString(input, StandardCharsets.UTF_8);
    Lexer lexer = new Lexer(source, input.toString());
    Parser parser = new Parser(lexer.tokenize());
    Node unit = parser.parse();
    new Sema().analyze(unit);
    return unit;
  }

  record Arguments(Path input, Path output, Path profile, Path remarks) {}
}
