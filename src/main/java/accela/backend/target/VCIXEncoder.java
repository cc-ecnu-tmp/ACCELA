package accela.backend.target;

import accela.backend.machine.VCIXInfo;

/** Pure encoder for the XSfVcp CUSTOM-2 instruction envelope. */
public final class VCIXEncoder {
  private VCIXEncoder() {}

  public static int encode(VCIXInfo info, int vd, int rs2, int rs1) {
    checkFiveBits("vd", vd);
    checkFiveBits("rs2", rs2);
    checkFiveBits("rs1", rs1);
    VCIXInfo.OperandForm form = info.form();
    int funct6Base =
        switch (form) {
          case X, I -> 0x00;
          case VV, XV, IV -> 0x08;
          case FV -> 0x0a;
          case VVV, XVV, IVV -> 0x28;
          case FVV -> 0x2a;
          case VVW, XVW, IVW -> 0x3c;
          case FVW -> 0x3e;
        };
    int funct3 =
        form.hasImmediate() ? 0b011
            : form.hasIntegerScalar() ? 0b100
            : form.hasFloatScalar() ? 0b101 : 0b000;
    int vm = info.writesVectorDestination() ? 0 : 1;
    return (funct6Base | info.functCustom()) << 26
        | vm << 25
        | rs2 << 20
        | rs1 << 15
        | funct3 << 12
        | vd << 7
        | 0x5b;
  }

  private static void checkFiveBits(String field, int value) {
    if (value < 0 || value > 31) {
      throw new IllegalArgumentException(field + " is outside its five-bit field");
    }
  }
}
