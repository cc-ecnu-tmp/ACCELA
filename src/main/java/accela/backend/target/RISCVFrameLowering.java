package accela.backend.target;

import accela.backend.frame.MachineFrameInfo;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegister;
import java.util.List;

public final class RISCVFrameLowering {
  // This register is allocatable in function bodies. The prologue runs before
  // any allocated value is defined, and the epilogue runs after RET has copied
  // its result to a0/fa0, so neither boundary has a live allocated t3 value.
  private static final String ABI_BOUNDARY_SCRATCH = "t3";

  private final RISCVTarget target;

  public RISCVFrameLowering(RISCVTarget target) {
    this.target = target;
  }

  void finalizeFrame(MachineFunction function) {
    function.getFrameInfo().finalizeLayout(target);
  }

  void emitPrologue(MachineFunction function, List<String> lines) {
    int frameSize = function.getFrameInfo().getFrameSize();
    if (frameSize > 0) {
      emitAddImmediate(lines, "sp", "sp", -frameSize, ABI_BOUNDARY_SCRATCH);
    }
    if (function.getFrameInfo().hasCalls()) {
      emitStoreToBase(
          lines,
          "ra",
          "sp",
          function.getFrameInfo().getSaveRaOffset(),
          ABI_BOUNDARY_SCRATCH,
          MachineType.PTR);
    }
    for (MachineFrameInfo.CalleeSavedRegisterSave save : function.getFrameInfo().getCalleeSavedRegisterSaves()) {
      PhysicalRegister register = save.getRegister();
      emitStoreToBase(
          lines,
          register.getName(),
          "sp",
          save.getOffset(),
          ABI_BOUNDARY_SCRATCH,
          register.getType().isFloat() ? "fsd" : "sd");
    }
  }

  void emitEpilogue(MachineFunction function, List<String> lines) {
    if (function.getFrameInfo().hasCalls()) {
      emitLoadFromBase(
          lines,
          "ra",
          "sp",
          function.getFrameInfo().getSaveRaOffset(),
          ABI_BOUNDARY_SCRATCH,
          MachineType.PTR);
    }
    for (MachineFrameInfo.CalleeSavedRegisterSave save : function.getFrameInfo().getCalleeSavedRegisterSaves()) {
      PhysicalRegister register = save.getRegister();
      emitLoadFromBase(
          lines,
          register.getName(),
          "sp",
          save.getOffset(),
          ABI_BOUNDARY_SCRATCH,
          register.getType().isFloat() ? "fld" : "ld");
    }
    int frameSize = function.getFrameInfo().getFrameSize();
    if (frameSize > 0) {
      emitAddImmediate(lines, "sp", "sp", frameSize, ABI_BOUNDARY_SCRATCH);
    }
    lines.add("  ret");
  }

  void emitLoadFromBase(
      List<String> lines, String dstReg, String baseReg, int offset, String scratchAddrReg, MachineType type) {
    emitLoadFromBase(lines, dstReg, baseReg, offset, scratchAddrReg, loadMnemonic(type));
  }

  private void emitLoadFromBase(
      List<String> lines, String dstReg, String baseReg, int offset, String scratchAddrReg, String op) {
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
    emitStoreToBase(lines, srcReg, baseReg, offset, scratchAddrReg, storeMnemonic(type));
  }

  private void emitStoreToBase(
      List<String> lines, String srcReg, String baseReg, int offset, String scratchAddrReg, String op) {
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
