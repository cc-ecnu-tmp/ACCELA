package accela.backend.target;

/** Scalar ISA selected independently from optional SIMD facilities. */
public enum ScalarISA {
  RV64GC("rv64gc");

  private final String architectureName;

  ScalarISA(String architectureName) {
    this.architectureName = architectureName;
  }

  public String architectureName() {
    return architectureName;
  }
}
