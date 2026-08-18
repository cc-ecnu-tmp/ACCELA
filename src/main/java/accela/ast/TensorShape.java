package accela.ast;

import java.util.Arrays;

/** Tensor-only shape metadata used before the AST is desugared to arrays of fixed vectors. */
public final class TensorShape {
  public static final int DYNAMIC = -1;

  public final Ty element;
  public final int[] dimensions;

  public TensorShape(Ty element, int... dimensions) {
    if (element != Ty.INT && element != Ty.FLOAT)
      throw new IllegalArgumentException("Tensor element type must be int or float");
    if (dimensions.length == 0) throw new IllegalArgumentException("Tensor rank must be positive");
    for (int dimension : dimensions) {
      if (dimension == 0 || dimension < DYNAMIC)
        throw new IllegalArgumentException("Tensor dimensions must be positive or dynamic");
    }
    this.element = element;
    this.dimensions = dimensions.clone();
  }

  public int rank() {
    return dimensions.length;
  }

  public int last() {
    return dimensions[dimensions.length - 1];
  }

  public boolean dynamic() {
    return Arrays.stream(dimensions).anyMatch(dimension -> dimension == DYNAMIC);
  }

  public int rows() {
    int rows = 1;
    for (int i = 0; i + 1 < dimensions.length; i++) {
      if (dimensions[i] == DYNAMIC) throw new IllegalStateException("dynamic Tensor row count");
      rows = Math.multiplyExact(rows, dimensions[i]);
    }
    return rows;
  }

  public int elements() {
    if (dynamic()) throw new IllegalStateException("dynamic Tensor element count");
    return Math.multiplyExact(rows(), last());
  }

  public TensorShape withElement(Ty type) {
    return type == element ? this : new TensorShape(type, dimensions);
  }

  public boolean sameTrailingDimensions(TensorShape other) {
    if (other == null || element != other.element || rank() != other.rank()) return false;
    for (int i = 1; i < dimensions.length; i++) {
      if (dimensions[i] != other.dimensions[i]) return false;
    }
    return true;
  }

  public boolean compatibleOuterDimensions(TensorShape other) {
    if (other == null || rank() != other.rank()) return false;
    for (int i = 0; i + 1 < dimensions.length; i++) {
      int left = dimensions[i], right = other.dimensions[i];
      if (left != DYNAMIC && right != DYNAMIC && left != right) return false;
    }
    return true;
  }

  @Override
  public boolean equals(Object object) {
    return object instanceof TensorShape other
        && element == other.element
        && Arrays.equals(dimensions, other.dimensions);
  }

  @Override
  public int hashCode() {
    return 31 * element.hashCode() + Arrays.hashCode(dimensions);
  }

  @Override
  public String toString() {
    StringBuilder result = new StringBuilder("tensor ").append(element);
    for (int dimension : dimensions) {
      result.append('[');
      if (dimension != DYNAMIC) result.append(dimension);
      result.append(']');
    }
    return result.toString();
  }
}
