package accela.backend.target;

/** The SIMD execution/register model layered on top of the scalar RISC-V ISA. */
public enum SIMDFeature {
  None(false, false),
  PackedFPR(false, false),
  PackedGPR(false, false),
  DedicatedVRF(true, false),
  StatefulCoprocessor(true, true);

  private final boolean hasVectorRegisterFile;
  private final boolean hasVCIX;

  SIMDFeature(boolean hasVectorRegisterFile, boolean hasVCIX) {
    this.hasVectorRegisterFile = hasVectorRegisterFile;
    this.hasVCIX = hasVCIX;
  }

  public boolean hasVectorRegisterFile() {
    return hasVectorRegisterFile;
  }

  public boolean hasRVV() {
    return hasVectorRegisterFile;
  }

  public boolean hasVCIX() {
    return hasVCIX;
  }

  public static SIMDFeature parse(String value) {
    return switch (value.toLowerCase()) {
      case "none" -> None;
      case "packed-fpr" -> PackedFPR;
      case "packed-gpr" -> PackedGPR;
      case "rvv", "v", "dedicated-vrf" -> DedicatedVRF;
      case "xsfvcp", "vcix", "stateful-coprocessor" -> StatefulCoprocessor;
      default -> throw new IllegalArgumentException("unsupported SIMD feature: " + value);
    };
  }
}
