package accela.cost;

import java.util.EnumMap;
import java.util.Map;
import java.util.NavigableMap;
import java.util.Objects;
import java.util.TreeMap;

/** Immutable, fully validated cost-model ABI. Runtime JSON loading is intentionally unsupported. */
public final class TargetProfile {
  public static final int SCHEMA_VERSION = 1;

  public enum EvidenceLevel { DECLARED, QEMU_PROXY, TARGET_HARDWARE }

  public record OperationCost(
      Measurement latency,
      Measurement reciprocalThroughput,
      double resourceOccupancy,
      int codeBytes,
      String resource) {
    public OperationCost {
      Objects.requireNonNull(latency, "latency");
      Objects.requireNonNull(reciprocalThroughput, "reciprocalThroughput");
      if (!Double.isFinite(resourceOccupancy) || resourceOccupancy <= 0.0) {
        throw new IllegalArgumentException("resource occupancy must be finite and positive");
      }
      if (codeBytes < 1) throw new IllegalArgumentException("codeBytes must be positive");
      if (resource == null || resource.isBlank()) throw new IllegalArgumentException("resource is required");
    }
  }

  public record DiagnosticCosts(
      Measurement loadUse,
      Measurement pointerChase,
      NavigableMap<Integer, Measurement> workingSet,
      NavigableMap<Integer, Measurement> stride,
      NavigableMap<Integer, Measurement> frontend,
      NavigableMap<Integer, Measurement> registerPressure) {
    public DiagnosticCosts {
      Objects.requireNonNull(loadUse, "loadUse");
      Objects.requireNonNull(pointerChase, "pointerChase");
      workingSet = checkedCurve(workingSet, "workingSet");
      stride = checkedCurve(stride, "stride");
      frontend = checkedCurve(frontend, "frontend");
      registerPressure = checkedCurve(registerPressure, "registerPressure");
    }

    private static NavigableMap<Integer, Measurement> checkedCurve(
        Map<Integer, Measurement> source, String name) {
      Objects.requireNonNull(source, name);
      TreeMap<Integer, Measurement> result = new TreeMap<>();
      source.forEach((point, measurement) -> {
        if (point == null || point <= 0) throw new IllegalArgumentException(name + " points must be positive");
        result.put(point, Objects.requireNonNull(measurement, name + " measurement"));
      });
      if (result.isEmpty()) throw new IllegalArgumentException(name + " curve is empty");
      return java.util.Collections.unmodifiableNavigableMap(result);
    }
  }

  private final String id;
  private final String isa;
  private final String abi;
  private final String codeModel;
  private final long clockHz;
  private final int fetchWidth;
  private final int issueWidth;
  private final int retireWidth;
  private final boolean calibrated;
  private final EvidenceLevel evidenceLevel;
  private final boolean simdEnabled;
  private final SchedulerPolicy scheduler;
  private final Map<InstructionClass, OperationCost> operations;
  private final Map<InstructionClass, Map<InstructionClass, Measurement>> pairing;
  private final Measurement predictableBranch;
  private final Measurement unpredictableBranch;
  private final Measurement spillLoad;
  private final Measurement spillStore;
  private final DiagnosticCosts diagnostics;

  private TargetProfile(Builder builder) {
    id = requireText(builder.id, "id");
    isa = requireText(builder.isa, "isa");
    abi = requireText(builder.abi, "abi");
    codeModel = requireText(builder.codeModel, "codeModel");
    if (builder.clockHz <= 0) throw new IllegalArgumentException("clockHz must be positive");
    clockHz = builder.clockHz;
    fetchWidth = requireWidth(builder.fetchWidth, "fetchWidth");
    issueWidth = requireWidth(builder.issueWidth, "issueWidth");
    retireWidth = requireWidth(builder.retireWidth, "retireWidth");
    calibrated = builder.calibrated;
    evidenceLevel = Objects.requireNonNull(builder.evidenceLevel, "evidenceLevel");
    if (calibrated == (evidenceLevel == EvidenceLevel.DECLARED)) {
      throw new IllegalArgumentException("calibrated and evidenceLevel are inconsistent");
    }
    simdEnabled = builder.simdEnabled;
    scheduler = Objects.requireNonNull(builder.scheduler, "scheduler");
    EnumMap<InstructionClass, OperationCost> copiedOps = new EnumMap<>(InstructionClass.class);
    copiedOps.putAll(builder.operations);
    for (InstructionClass instructionClass : InstructionClass.values()) {
      if (!copiedOps.containsKey(instructionClass)) {
        throw new IllegalArgumentException("missing operation cost for " + instructionClass);
      }
    }
    operations = Map.copyOf(copiedOps);
    EnumMap<InstructionClass, Map<InstructionClass, Measurement>> copiedPairing =
        new EnumMap<>(InstructionClass.class);
    for (InstructionClass left : InstructionClass.values()) {
      EnumMap<InstructionClass, Measurement> row = new EnumMap<>(InstructionClass.class);
      Map<InstructionClass, Measurement> source = builder.pairing.get(left);
      if (source != null) row.putAll(source);
      for (InstructionClass right : InstructionClass.values()) {
        Measurement value = row.get(right);
        Measurement reverse = builder.pairing.getOrDefault(right, Map.of()).get(left);
        if (value == null || reverse == null) {
          throw new IllegalArgumentException("incomplete pairing matrix at " + left + "/" + right);
        }
        if (!value.equals(reverse)) {
          throw new IllegalArgumentException("asymmetric pairing matrix at " + left + "/" + right);
        }
      }
      copiedPairing.put(left, Map.copyOf(row));
    }
    pairing = Map.copyOf(copiedPairing);
    predictableBranch = Objects.requireNonNull(builder.predictableBranch, "predictableBranch");
    unpredictableBranch = Objects.requireNonNull(builder.unpredictableBranch, "unpredictableBranch");
    spillLoad = Objects.requireNonNull(builder.spillLoad, "spillLoad");
    spillStore = Objects.requireNonNull(builder.spillStore, "spillStore");
    diagnostics = Objects.requireNonNull(builder.diagnostics, "diagnostics");
  }

  public String id() { return id; }
  public String isa() { return isa; }
  public String abi() { return abi; }
  public String codeModel() { return codeModel; }
  public long clockHz() { return clockHz; }
  public int fetchWidth() { return fetchWidth; }
  public int issueWidth() { return issueWidth; }
  public int retireWidth() { return retireWidth; }
  public boolean calibrated() { return calibrated; }
  public EvidenceLevel evidenceLevel() { return evidenceLevel; }
  public boolean simdEnabled() { return simdEnabled; }
  public SchedulerPolicy scheduler() { return scheduler; }
  public OperationCost operation(InstructionClass instructionClass) { return operations.get(instructionClass); }
  public Measurement pairing(InstructionClass left, InstructionClass right) { return pairing.get(left).get(right); }
  public Measurement predictableBranch() { return predictableBranch; }
  public Measurement unpredictableBranch() { return unpredictableBranch; }
  public Measurement spillLoad() { return spillLoad; }
  public Measurement spillStore() { return spillStore; }
  public DiagnosticCosts diagnostics() { return diagnostics; }

  public static Builder builder() { return new Builder(); }

  private static String requireText(String value, String name) {
    if (value == null || value.isBlank()) throw new IllegalArgumentException(name + " is required");
    return value;
  }

  private static int requireWidth(int value, String name) {
    if (value < 1 || value > 16) throw new IllegalArgumentException(name + " must be 1..16");
    return value;
  }

  public static final class Builder {
    private String id;
    private String isa;
    private String abi;
    private String codeModel;
    private long clockHz;
    private int fetchWidth;
    private int issueWidth;
    private int retireWidth;
    private boolean calibrated;
    private EvidenceLevel evidenceLevel;
    private boolean simdEnabled;
    private SchedulerPolicy scheduler;
    private final EnumMap<InstructionClass, OperationCost> operations = new EnumMap<>(InstructionClass.class);
    private final EnumMap<InstructionClass, Map<InstructionClass, Measurement>> pairing = new EnumMap<>(InstructionClass.class);
    private Measurement predictableBranch;
    private Measurement unpredictableBranch;
    private Measurement spillLoad;
    private Measurement spillStore;
    private DiagnosticCosts diagnostics;

    public Builder identity(String id, String isa, String abi, String codeModel) {
      this.id = id; this.isa = isa; this.abi = abi; this.codeModel = codeModel; return this;
    }
    public Builder core(long clockHz, int fetchWidth, int issueWidth, int retireWidth) {
      this.clockHz = clockHz; this.fetchWidth = fetchWidth; this.issueWidth = issueWidth;
      this.retireWidth = retireWidth; return this;
    }
    public Builder capabilities(boolean calibrated, EvidenceLevel evidenceLevel, boolean simdEnabled) {
      this.calibrated = calibrated; this.evidenceLevel = evidenceLevel;
      this.simdEnabled = simdEnabled; return this;
    }
    public Builder scheduler(SchedulerPolicy scheduler) { this.scheduler = scheduler; return this; }
    public Builder operation(InstructionClass instructionClass, OperationCost cost) {
      operations.put(instructionClass, cost); return this;
    }
    public Builder pair(InstructionClass left, InstructionClass right, Measurement cost) {
      pairing.computeIfAbsent(left, ignored -> new EnumMap<>(InstructionClass.class)).put(right, cost);
      pairing.computeIfAbsent(right, ignored -> new EnumMap<>(InstructionClass.class)).put(left, cost);
      return this;
    }
    public Builder branch(Measurement predictable, Measurement unpredictable) {
      predictableBranch = predictable; unpredictableBranch = unpredictable; return this;
    }
    public Builder spills(Measurement load, Measurement store) {
      spillLoad = load; spillStore = store; return this;
    }
    public Builder diagnostics(DiagnosticCosts costs) { diagnostics = costs; return this; }
    public TargetProfile build() { return new TargetProfile(this); }
  }
}
