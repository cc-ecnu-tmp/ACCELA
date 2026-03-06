package accela.ir;

/**
 * A global variable or constant. Extends Value so it can be used
 * as an operand in load/store/GEP instructions.
 */
public class GlobalVariable extends Value {
  private final Constant initializer;
  private final boolean isConstant;

  public GlobalVariable(String name, Type type, Constant initializer, boolean isConstant) {
    super(Type.PTR, name); // globals have pointer type (address of the variable)
    this.initializer = initializer;
    this.isConstant = isConstant;
  }

  /** The type of the stored value (not the pointer type). */
  public Type getValueType() {
    return initializer != null ? initializer.getType() : Type.INT;
  }

  public Constant getInitializer() {
    return initializer;
  }

  public boolean isConstant() {
    return isConstant;
  }
}
