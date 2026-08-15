package accela.pass;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Immutable legal pass sequence; phase and dependency violations fail before compilation. */
public final class PipelineProfile {
  private final PassRegistry registry;
  private final List<String> enabled;

  public PipelineProfile(PassRegistry registry, List<String> enabled) {
    this.registry = java.util.Objects.requireNonNull(registry, "registry");
    this.enabled = List.copyOf(enabled);
    Set<String> seen = new HashSet<>();
    int lastIndex = -1;
    for (String id : this.enabled) {
      if (!seen.add(id)) throw new IllegalArgumentException("duplicate enabled pass: " + id);
      PassDescriptor descriptor = registry.require(id);
      int index = registry.all().indexOf(descriptor);
      if (index <= lastIndex) throw new IllegalArgumentException("enabled passes violate registry order");
      lastIndex = index;
      for (String dependency : descriptor.dependencies()) {
        if (!seen.contains(dependency)) {
          throw new IllegalArgumentException("enabled pass misses dependency " + dependency);
        }
      }
    }
    for (PassDescriptor descriptor : registry.all()) {
      if (descriptor.required() && !seen.contains(descriptor.id())) {
        throw new IllegalArgumentException("pipeline misses required pass " + descriptor.id());
      }
    }
  }

  public static PipelineProfile r1() {
    PassRegistry registry = PassRegistry.r1();
    return new PipelineProfile(registry, registry.all().stream().map(PassDescriptor::id).toList());
  }

  public List<String> enabled() { return enabled; }
  public PassDescriptor require(String id) {
    if (!enabled.contains(id)) throw new IllegalArgumentException("pass is disabled: " + id);
    return registry.require(id);
  }
}
