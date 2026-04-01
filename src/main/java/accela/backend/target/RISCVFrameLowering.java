package accela.backend;

import java.util.List;

final class RISCVFrameLowering {
  private final RISCVTarget target;

  RISCVFrameLowering(RISCVTarget target) {
    this.target = target;
  }

  void finalizeFrame(MachineFunction function) {
    function.getFrameInfo().finalizeLayout(target);
  }

  void emitPrologue(MachineFunction function, List<String> lines) {
    int frameSize = function.getFrameInfo().getFrameSize();
    if (frameSize > 0) {
      emitAddImmediate(lines, "sp", "sp", -frameSize, "t3");
    }
    emitStoreToBase(lines, "s0", "sp", function.getFrameInfo().getSaveS0Offset(), "t3", MachineType.PTR);
    if (function.getFrameInfo().hasCalls()) {
      emitStoreToBase(lines, "ra", "sp", function.getFrameInfo().getSaveRaOffset(), "t3", MachineType.PTR);
    }
    if (frameSize > 0) {
      emitAddImmediate(lines, "s0", "sp", frameSize, "t3");
    } else {
      lines.add("  mv s0, sp");
    }
  }

  void emitEpilogue(MachineFunction function, List<String> lines) {
    emitLoadFromBase(lines, "s0", "sp", function.getFrameInfo().getSaveS0Offset(), "t3", MachineType.PTR);
    if (function.getFrameInfo().hasCalls()) {
      emitLoadFromBase(lines, "ra", "sp", function.getFrameInfo().getSaveRaOffset(), "t3", MachineType.PTR);
    }
    int frameSize = function.getFrameInfo().getFrameSize();
    if (frameSize > 0) {
      emitAddImmediate(lines, "sp", "sp", frameSize, "t3");
    }
    lines.add("  ret");
  }

  void emitLoadFromBase(
      List<String> lines, String dstReg, String baseReg, int offset, String scratchAddrReg, MachineType type) {
    String op = loadMnemonic(type);
    if (fitsImm12(offset)) {
      lines.add("  " + op + " " + dstReg + ", " + offset + "(" + baseReg + ")");
    } else {
      lines.add("  li " + scratchAddrReg + ", " + offset);
      lines.add("  add " + scratchAddrReg + ", " + baseReg + ", " + scratchAddrReg);
      lines.add("  " + op + " " + dstReg + ", 0(" + scratchAddrReg + ")");
    }
  }

  void emitStoreToBase(
      List<String> lines, String srcReg, String baseReg, int offset, String scratchAddrReg, MachineType type) {
    String op = storeMnemonic(type);
    if (fitsImm12(offset)) {
      lines.add("  " + op + " " + srcReg + ", " + offset + "(" + baseReg + ")");
    } else {
      lines.add("  li " + scratchAddrReg + ", " + offset);
      lines.add("  add " + scratchAddrReg + ", " + baseReg + ", " + scratchAddrReg);
      lines.add("  " + op + " " + srcReg + ", 0(" + scratchAddrReg + ")");
    }
  }

  void emitAddImmediate(
      List<String> lines, String dstReg, String baseReg, int offset, String scratchAddrReg) {
    if (offset == 0) {
      lines.add("  mv " + dstReg + ", " + baseReg);
    } else if (fitsImm12(offset)) {
      lines.add("  addi " + dstReg + ", " + baseReg + ", " + offset);
    } else {
      lines.add("  li " + scratchAddrReg + ", " + offset);
      lines.add("  add " + dstReg + ", " + baseReg + ", " + scratchAddrReg);
    }
  }

  String loadMnemonic(MachineType type) {
    if (type.isFloat()) return "flw";
    if (type == MachineType.PTR || type == MachineType.I64) return "ld";
    return "lw";
  }

  String storeMnemonic(MachineType type) {
    if (type.isFloat()) return "fsw";
    if (type == MachineType.PTR || type == MachineType.I64) return "sd";
    return "sw";
  }

  private boolean fitsImm12(int value) {
    return value >= -2048 && value <= 2047;
  }
}
