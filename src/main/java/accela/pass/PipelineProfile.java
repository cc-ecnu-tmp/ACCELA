package accela.pass;

import accela.util.StrictJson;
import java.io.IOException;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/** Immutable, serializable compiler pipeline profile. */
public final class PipelineProfile {
  public enum Base {
    FULL,
    MANDATORY_ONLY
  }

  public static final int SCHEMA_VERSION = 2;
  private static final long MAX_PROFILE_BYTES = 1L << 20;
  private static final Set<String> TOP_LEVEL_KEYS =
      Set.of("schema_version", "base", "disable", "enable_candidates");
  private static final Set<String> DISABLE_KEYS = Set.of("pass", "family", "occurrence");

  private final PassRegistry registry;
  private final Base base;
  private final Set<String> disabledFamilies;
  private final Set<String> disabledPasses;
  private final Map<String, Set<Integer>> disabledOccurrences;
  private final List<String> enabledCandidates;

  private PipelineProfile(
      PassRegistry registry,
      Base base,
      Set<String> disabledFamilies,
      Set<String> disabledPasses,
      Map<String, Set<Integer>> disabledOccurrences,
      List<String> enabledCandidates) {
    this.registry = Objects.requireNonNull(registry, "registry");
    this.base = Objects.requireNonNull(base, "base");
    this.disabledFamilies = immutableSet(disabledFamilies);
    this.disabledPasses = immutableSet(disabledPasses);
    LinkedHashMap<String, Set<Integer>> copied = new LinkedHashMap<>();
    disabledOccurrences.forEach(
        (pass, occurrences) -> copied.put(pass, immutableSet(occurrences)));
    this.disabledOccurrences = Collections.unmodifiableMap(copied);
    this.enabledCandidates = List.copyOf(enabledCandidates);
  }

  public static PipelineProfile full() {
    return full(PassRegistry.standard());
  }

  public static PipelineProfile full(PassRegistry registry) {
    return new PipelineProfile(registry, Base.FULL, Set.of(), Set.of(), Map.of(), List.of());
  }

  public static PipelineProfile mandatoryOnly() {
    return mandatoryOnly(PassRegistry.standard());
  }

  public static PipelineProfile mandatoryOnly(PassRegistry registry) {
    return new PipelineProfile(
        registry, Base.MANDATORY_ONLY, Set.of(), Set.of(), Map.of(), List.of());
  }

  public static PipelineProfile fromJson(Path path) throws IOException {
    return fromJson(path, PassRegistry.standard());
  }

  public static PipelineProfile fromJson(Path path, PassRegistry registry) throws IOException {
    Objects.requireNonNull(path, "path");
    long size = Files.size(path);
    if (size > MAX_PROFILE_BYTES) {
      throw new IllegalArgumentException("pipeline profile exceeds " + MAX_PROFILE_BYTES + " bytes");
    }
    return fromJson(Files.readString(path, StandardCharsets.UTF_8), registry);
  }

  public static PipelineProfile fromJson(String json) {
    return fromJson(json, PassRegistry.standard());
  }

  public static PipelineProfile fromJson(String json, PassRegistry registry) {
    Object parsed = StrictJson.parse(json);
    Map<String, Object> root = object(parsed, "pipeline profile");
    rejectUnknownKeys(root, TOP_LEVEL_KEYS, "pipeline profile");
    requireInteger(root.get("schema_version"), "schema_version", SCHEMA_VERSION);
    Base base = parseBase(string(root.get("base"), "base"));
    Object disabledValue = root.get("disable");
    if (!(disabledValue instanceof List<?> disabled)) {
      throw new IllegalArgumentException("disable must be an array");
    }
    Object enabledValue = root.get("enable_candidates");
    if (!(enabledValue instanceof List<?> enabledCandidates)) {
      throw new IllegalArgumentException("enable_candidates must be an array");
    }

    Builder builder = builder(registry, base);
    for (int index = 0; index < disabled.size(); index++) {
      String entryName = "disable[" + index + "]";
      Map<String, Object> entry = object(disabled.get(index), entryName);
      rejectUnknownKeys(entry, DISABLE_KEYS, entryName);
      boolean hasPass = entry.containsKey("pass");
      boolean hasFamily = entry.containsKey("family");
      if (hasPass == hasFamily) {
        throw new IllegalArgumentException(
            entryName + " must contain exactly one of pass or family");
      }
      if (hasFamily) {
        if (entry.containsKey("occurrence")) {
          throw new IllegalArgumentException(entryName + ".occurrence is only valid with pass");
        }
        builder.disableFamily(string(entry.get("family"), entryName + ".family"));
      } else {
        String pass = string(entry.get("pass"), entryName + ".pass");
        if (entry.containsKey("occurrence")) {
          builder.disableOccurrence(pass,
              positiveInteger(entry.get("occurrence"), entryName + ".occurrence"));
        } else {
          builder.disable(pass);
        }
      }
    }
    for (int index = 0; index < enabledCandidates.size(); index++) {
      builder.enableCandidate(
          string(enabledCandidates.get(index), "enable_candidates[" + index + "]"));
    }
    return builder.build();
  }

  public static Builder builder() {
    return builder(PassRegistry.standard(), Base.FULL);
  }

  public static Builder builder(PassRegistry registry) {
    return builder(registry, Base.FULL);
  }

  public static Builder builder(Base base) {
    return builder(PassRegistry.standard(), base);
  }

  public static Builder builder(PassRegistry registry, Base base) {
    return new Builder(registry, base);
  }

  public boolean isEnabled(String passId, int occurrence) {
    PassDescriptor descriptor = registry.require(passId);
    validateOccurrence(descriptor, occurrence);
    if (descriptor.candidate()) return enabledCandidates.contains(passId);
    if (descriptor.required()) return true;
    if (base == Base.MANDATORY_ONLY) return false;
    return !disabledFamilies.contains(descriptor.logicalFamilyId())
        && !disabledPasses.contains(passId)
        && !disabledOccurrences.getOrDefault(passId, Set.of()).contains(occurrence);
  }

  public boolean disablesAll(String passId) {
    PassDescriptor descriptor = registry.require(passId);
    if (descriptor.candidate()) return !enabledCandidates.contains(passId);
    return !descriptor.required() && (base == Base.MANDATORY_ONLY
        || disabledFamilies.contains(descriptor.logicalFamilyId())
        || disabledPasses.contains(passId)
        || disabledOccurrences.getOrDefault(passId, Set.of()).size()
            == descriptor.fullPipelineOccurrences());
  }

  public Set<Integer> disabledOccurrences(String passId) {
    registry.require(passId);
    return disabledOccurrences.getOrDefault(passId, Set.of());
  }

  public Set<String> disabledFamilies() {
    return disabledFamilies;
  }

  /** Candidate pass ids in their canonical PassRegistry scheduling order. */
  public List<String> enabledCandidates() {
    return enabledCandidates;
  }

  public Base base() {
    return base;
  }

  public PassRegistry registry() {
    return registry;
  }

  /** Returns a canonical JSON representation suitable for checked-in benchmark profiles. */
  public String toJson() {
    LinkedHashMap<String, Object> root = new LinkedHashMap<>();
    root.put("schema_version", SCHEMA_VERSION);
    root.put("base", base.name());
    List<Map<String, Object>> disabled = new ArrayList<>();
    for (String family : registry.families()) {
      if (disabledFamilies.contains(family)) disabled.add(Map.of("family", family));
    }
    for (PassDescriptor descriptor : registry.all()) {
      String pass = descriptor.id();
      if (disabledPasses.contains(pass)) disabled.add(Map.of("pass", pass));
    }
    for (PassDescriptor descriptor : registry.all()) {
      String pass = descriptor.id();
      for (int occurrence : disabledOccurrences.getOrDefault(pass, Set.of()).stream().sorted().toList()) {
        LinkedHashMap<String, Object> entry = new LinkedHashMap<>();
        entry.put("pass", pass);
        entry.put("occurrence", occurrence);
        disabled.add(Collections.unmodifiableMap(entry));
      }
    }
    root.put("disable", disabled);
    root.put("enable_candidates", enabledCandidates);
    return StrictJson.stringify(root);
  }

  public void writeJson(Path path) throws IOException {
    Objects.requireNonNull(path, "path");
    Files.writeString(path, toJson() + "\n", StandardCharsets.UTF_8);
  }

  private static Map<String, Object> object(Object value, String name) {
    if (!(value instanceof Map<?, ?> raw)) throw new IllegalArgumentException(name + " must be an object");
    LinkedHashMap<String, Object> result = new LinkedHashMap<>();
    for (Map.Entry<?, ?> entry : raw.entrySet()) {
      if (!(entry.getKey() instanceof String key)) {
        throw new IllegalArgumentException(name + " contains a non-string key");
      }
      result.put(key, entry.getValue());
    }
    return result;
  }

  private static void rejectUnknownKeys(Map<String, Object> object, Set<String> allowed, String name) {
    for (String key : object.keySet()) {
      if (!allowed.contains(key)) throw new IllegalArgumentException(name + " contains unknown key '" + key + "'");
    }
  }

  private static String string(Object value, String name) {
    if (!(value instanceof String text) || text.isBlank()) {
      throw new IllegalArgumentException(name + " must be a non-empty string");
    }
    return text;
  }

  private static Base parseBase(String name) {
    try {
      return Base.valueOf(name);
    } catch (IllegalArgumentException exception) {
      throw new IllegalArgumentException("unsupported pipeline base '" + name + "'", exception);
    }
  }

  private static int positiveInteger(Object value, String name) {
    if (!(value instanceof BigDecimal number)) throw new IllegalArgumentException(name + " must be an integer");
    try {
      int result = number.intValueExact();
      if (result < 1) throw new IllegalArgumentException(name + " must be positive");
      return result;
    } catch (ArithmeticException exception) {
      throw new IllegalArgumentException(name + " must be an integer", exception);
    }
  }

  private static void requireInteger(Object value, String name, int expected) {
    int actual = positiveInteger(value, name);
    if (actual != expected) {
      throw new IllegalArgumentException(
          "unsupported " + name + " " + actual + "; expected " + expected);
    }
  }

  private static void validateOccurrence(PassDescriptor descriptor, int occurrence) {
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException(
          "unknown occurrence " + occurrence + " for pass '" + descriptor.id()
              + "'; valid range is 1.." + descriptor.fullPipelineOccurrences());
    }
  }

  private static <T> Set<T> immutableSet(Set<T> source) {
    return Collections.unmodifiableSet(new LinkedHashSet<>(source));
  }

  public static final class Builder {
    private final PassRegistry registry;
    private final Base base;
    private final Set<String> disabledFamilies = new LinkedHashSet<>();
    private final Set<String> disabledPasses = new LinkedHashSet<>();
    private final Map<String, Set<Integer>> disabledOccurrences = new LinkedHashMap<>();
    private final Set<String> enabledCandidates = new LinkedHashSet<>();

    private Builder(PassRegistry registry, Base base) {
      this.registry = Objects.requireNonNull(registry, "registry");
      this.base = Objects.requireNonNull(base, "base");
    }

    public Builder disableFamily(String familyId) {
      requireFullBase();
      List<PassDescriptor> family = registry.forFamily(familyId);
      if (family.stream().anyMatch(PassDescriptor::candidate)) {
        throw new IllegalArgumentException(
            "candidate pass families cannot be disabled: '" + familyId + "'");
      }
      if (family.stream().anyMatch(PassDescriptor::required)) {
        throw new IllegalArgumentException(
            "pass family '" + familyId + "' contains a required pass and cannot be disabled");
      }
      if (family.stream().anyMatch(pass -> disabledPasses.contains(pass.id())
          || disabledOccurrences.containsKey(pass.id()))) {
        throw new IllegalArgumentException(
            "pass family '" + familyId + "' conflicts with an existing pass disable entry");
      }
      if (!disabledFamilies.add(familyId)) {
        throw new IllegalArgumentException("duplicate disable entry for pass family '" + familyId + "'");
      }
      return this;
    }

    public Builder disable(String passId) {
      requireFullBase();
      PassDescriptor descriptor = configurable(passId);
      rejectDisabledFamily(descriptor);
      if (disabledOccurrences.containsKey(passId)) {
        throw new IllegalArgumentException(
            "pass '" + passId + "' cannot be disabled both globally and by occurrence");
      }
      if (!disabledPasses.add(descriptor.id())) {
        throw new IllegalArgumentException("duplicate disable entry for pass '" + passId + "'");
      }
      return this;
    }

    public Builder disableOccurrence(String passId, int occurrence) {
      requireFullBase();
      PassDescriptor descriptor = configurable(passId);
      validateOccurrence(descriptor, occurrence);
      rejectDisabledFamily(descriptor);
      if (disabledPasses.contains(passId)) {
        throw new IllegalArgumentException(
            "pass '" + passId + "' cannot be disabled both globally and by occurrence");
      }
      Set<Integer> occurrences =
          disabledOccurrences.computeIfAbsent(passId, ignored -> new LinkedHashSet<>());
      if (!occurrences.add(occurrence)) {
        throw new IllegalArgumentException(
            "duplicate disable entry for pass '" + passId + "' occurrence " + occurrence);
      }
      return this;
    }

    public Builder enableCandidate(String passId) {
      requireFullBase();
      PassDescriptor descriptor = registry.require(passId);
      if (!descriptor.candidate()) {
        throw new IllegalArgumentException(
            "only candidate passes may be explicitly enabled: '" + passId + "'");
      }
      if (!enabledCandidates.add(passId)) {
        throw new IllegalArgumentException(
            "duplicate enable entry for candidate pass '" + passId + "'");
      }
      return this;
    }

    public PipelineProfile build() {
      List<String> requestedOrder = List.copyOf(enabledCandidates);
      List<String> registryOrder = registry.candidates().stream()
          .map(PassDescriptor::id)
          .filter(enabledCandidates::contains)
          .toList();
      if (!requestedOrder.equals(registryOrder)) {
        throw new IllegalArgumentException(
            "candidate enable entries must follow PassRegistry order; expected "
                + registryOrder + ", but found " + requestedOrder);
      }
      return new PipelineProfile(
          registry, base, disabledFamilies, disabledPasses, disabledOccurrences,
          registryOrder);
    }

    private PassDescriptor configurable(String passId) {
      PassDescriptor descriptor = registry.require(passId);
      if (descriptor.required()) {
        throw new IllegalArgumentException("required pass '" + passId + "' cannot be disabled");
      }
      if (descriptor.candidate()) {
        throw new IllegalArgumentException(
            "candidate pass '" + passId + "' is default-off and cannot be disabled");
      }
      return descriptor;
    }

    private void rejectDisabledFamily(PassDescriptor descriptor) {
      if (disabledFamilies.contains(descriptor.logicalFamilyId())) {
        throw new IllegalArgumentException("pass '" + descriptor.id()
            + "' conflicts with disabled family '" + descriptor.logicalFamilyId() + "'");
      }
    }

    private void requireFullBase() {
      if (base != Base.FULL) {
        throw new IllegalArgumentException(
            "MANDATORY_ONLY profiles cannot contain redundant disable entries");
      }
    }
  }
}
