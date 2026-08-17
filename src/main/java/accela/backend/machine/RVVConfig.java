package accela.backend.machine;

/** Required architectural vector state for one RVV or VCIX instruction. */
public record RVVConfig(
    int avl, int sew, int lmul, TailPolicy tailPolicy, MaskPolicy maskPolicy) {
  public enum TailPolicy {
    AGNOSTIC("ta"),
    UNDISTURBED("tu");

    private final String assemblyName;

    TailPolicy(String assemblyName) {
      this.assemblyName = assemblyName;
    }

    public String assemblyName() {
      return assemblyName;
    }
  }

  public enum MaskPolicy {
    AGNOSTIC("ma"),
    UNDISTURBED("mu");

    private final String assemblyName;

    MaskPolicy(String assemblyName) {
      this.assemblyName = assemblyName;
    }

    public String assemblyName() {
      return assemblyName;
    }
  }

  public RVVConfig {
    if (avl < 0) throw new IllegalArgumentException("AVL cannot be negative");
    if (sew != 8 && sew != 16 && sew != 32 && sew != 64) {
      throw new IllegalArgumentException("unsupported SEW: " + sew);
    }
    if (lmul != 1 && lmul != 2 && lmul != 4 && lmul != 8) {
      throw new IllegalArgumentException("unsupported LMUL: " + lmul);
    }
  }

  public static RVVConfig forShape(VectorShape shape) {
    return new RVVConfig(
        shape.lanes(),
        shape.sew(),
        shape.stateLmul(),
        TailPolicy.AGNOSTIC,
        MaskPolicy.AGNOSTIC);
  }

  public String vtypeAssembly() {
    return "e" + sew + ", m" + lmul + ", " + tailPolicy.assemblyName() + ", "
        + maskPolicy.assemblyName();
  }
}
