package accela.pass.ir.analysis.alias;

import accela.ir.Type;
import accela.ir.Value;
import java.util.List;

/** Immutable SysY array facts retained for region-aware transformations. */
public record ArrayProvenance(
    Value root,
    Type elementType,
    List<Integer> shape,
    List<Long> strides,
    MemoryRegion region) {
  public ArrayProvenance {
    if (root == null || elementType == null || region == null) {
      throw new NullPointerException("array provenance fields must not be null");
    }
    shape = List.copyOf(shape);
    strides = List.copyOf(strides);
  }

  public boolean exact() {
    return region.exact();
  }
}
