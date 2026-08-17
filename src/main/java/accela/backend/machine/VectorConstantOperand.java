package accela.backend.machine;

import accela.ir.Constant;
import java.util.List;

/** A fixed vector constant waiting to be materialized by target emission. */
public final class VectorConstantOperand extends MachineOperand {
  private final VectorShape shape;
  private final List<Constant> elements;

  public VectorConstantOperand(VectorShape shape, List<Constant> elements) {
    super(Kind.VECTOR_CONSTANT);
    this.shape = shape;
    this.elements = List.copyOf(elements);
    if (elements.size() != shape.lanes()) {
      throw new IllegalArgumentException("vector constant lane count mismatch");
    }
  }

  public VectorShape getShape() {
    return shape;
  }

  public List<Constant> getElements() {
    return elements;
  }
}
