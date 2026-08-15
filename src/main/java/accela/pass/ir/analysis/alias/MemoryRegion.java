package accela.pass.ir.analysis.alias;

import accela.ir.Value;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** A conservative byte interval rooted at one SysY object. */
public record MemoryRegion(
    Value root,
    long byteOffset,
    long byteWidth,
    boolean exact,
    List<Long> indices) {
  public MemoryRegion {
    if (byteOffset < 0 || byteWidth <= 0) {
      throw new IllegalArgumentException("memory region must be a positive non-negative interval");
    }
    if (indices == null) {
      throw new IllegalArgumentException("memory region indices must be present");
    }
    indices = Collections.unmodifiableList(new ArrayList<>(indices));
  }

  public boolean disjoint(MemoryRegion other) {
    if (other == null || root != other.root || !exact || !other.exact) return false;
    final long end;
    final long otherEnd;
    try {
      end = Math.addExact(byteOffset, byteWidth);
      otherEnd = Math.addExact(other.byteOffset, other.byteWidth);
    } catch (ArithmeticException overflow) {
      return false;
    }
    return end <= other.byteOffset || otherEnd <= byteOffset;
  }
}
