package accela.backend.machine;

/** Encoding envelope and custom-use bits for one XSfVcp/VCIX instruction. */
public record VCIXInfo(
    OperandForm form,
    boolean writesVectorDestination,
    int functCustom,
    int rs2Custom,
    int rdCustom,
    boolean sideEffect) {
  public enum OperandForm {
    X,
    I,
    VV,
    XV,
    IV,
    FV,
    VVV,
    XVV,
    IVV,
    FVV,
    VVW,
    XVW,
    IVW,
    FVW;

    public boolean hasVectorSource2() {
      return this != X && this != I;
    }

    public boolean hasVectorSource1() {
      return this == VV || this == VVV || this == VVW;
    }

    public boolean hasIntegerScalar() {
      return this == X || this == XV || this == XVV || this == XVW;
    }

    public boolean hasFloatScalar() {
      return this == FV || this == FVV || this == FVW;
    }

    public boolean hasImmediate() {
      return this == I || this == IV || this == IVV || this == IVW;
    }

    public boolean readsDestination() {
      return name().length() == 3;
    }

    public boolean isWidening() {
      return name().endsWith("W");
    }
  }

  public VCIXInfo {
    if (functCustom < 0 || functCustom > (form.hasFloatScalar() ? 1 : 3)) {
      throw new IllegalArgumentException("VCIX funct custom field is out of range");
    }
    if (rs2Custom < 0 || rs2Custom > 31 || rdCustom < 0 || rdCustom > 31) {
      throw new IllegalArgumentException("VCIX custom register field is out of range");
    }
  }

  public String mnemonic() {
    String suffix = form.name().toLowerCase();
    return "sf.vc." + (writesVectorDestination ? "v." : "") + suffix;
  }
}
