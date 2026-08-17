package accela.backend.target;

/** Immutable target selection used to construct one backend pipeline. */
public record RISCVTargetOptions(ScalarISA scalarISA, SIMDFeature simdFeature, int minimumVLEN) {
  public static final int DEFAULT_VLEN = 128;

  public RISCVTargetOptions {
    if (scalarISA == null || simdFeature == null) {
      throw new IllegalArgumentException("target ISA and SIMD feature are required");
    }
    if (minimumVLEN < 128 || (minimumVLEN & (minimumVLEN - 1)) != 0) {
      throw new IllegalArgumentException("RISC-V VLEN must be a power of two of at least 128");
    }
  }

  public static RISCVTargetOptions scalarDefault() {
    return new RISCVTargetOptions(ScalarISA.RV64GC, SIMDFeature.None, DEFAULT_VLEN);
  }

  public static RISCVTargetOptions of(String simd, int minimumVLEN) {
    return new RISCVTargetOptions(ScalarISA.RV64GC, SIMDFeature.parse(simd), minimumVLEN);
  }
}
