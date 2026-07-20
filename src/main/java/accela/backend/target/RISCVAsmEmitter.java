package accela.backend.target;

import accela.backend.frame.StackSlot;
import accela.backend.machine.BlockOperand;
import accela.backend.machine.FloatImmOperand;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegOperand;
import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.StackSlotOperand;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterLocation;
import accela.backend.regalloc.StackLocation;
import accela.backend.regalloc.ValueLocation;
import java.util.List;

public final class RISCVAsmEmitter {
  private final RISCVTarget target;
  private final RISCVFrameLowering frameLowering;

  public RISCVAsmEmitter(RISCVTarget target, RISCVFrameLowering frameLowering) {
    this.target = target;
    this.frameLowering = frameLowering;
  }

  void emitInstruction(
      MachineFunction function,
      MachineInstr instr,
      AllocationResult allocation,
      List<String> lines) {
    switch (instr.getOpcode()) {
      case ARG_IN:
        emitArgIn(instr, allocation, lines);
        return;
      case CONST_INT:
        materializeInto(lines, instr.getOperands().get(0), "t0", instr.getType(), allocation);
        writeDest(lines, instr.getDest(), "t0", allocation, instr.getType());
        return;
      case STACK_ADDR:
        StackSlot slot = ((StackSlotOperand) instr.getOperands().get(0)).getSlot();
        frameLowering.emitAddImmediate(lines, "t0", "sp", slot.getOffset(), "t3");
        writeDest(lines, instr.getDest(), "t0", allocation, instr.getType());
        return;
      case MOVE:
        materializeInto(
            lines,
            instr.getOperands().get(0),
            instr.getType().isFloat() ? "ft0" : "t0",
            inferOperandType(instr.getOperands().get(0)),
            allocation);
        writeDest(lines, instr.getDest(), instr.getType().isFloat() ? "ft0" : "t0", allocation, instr.getType());
        return;
      case ZEXT:
      case SEXT:
        materializeInto(lines, instr.getOperands().get(0), "t0", inferOperandType(instr.getOperands().get(0)), allocation);
        writeDest(lines, instr.getDest(), "t0", allocation, instr.getType());
        return;
      case SITOFP:
        materializeInto(lines, instr.getOperands().get(0), "t0", MachineType.I32, allocation);
        lines.add("  fcvt.s.w ft0, t0");
        writeDest(lines, instr.getDest(), "ft0", allocation, MachineType.F32);
        return;
      case FPTOSI:
        materializeInto(lines, instr.getOperands().get(0), "ft0", MachineType.F32, allocation);
        lines.add("  fcvt.w.s t0, ft0, rtz");
        writeDest(lines, instr.getDest(), "t0", allocation, MachineType.I32);
        return;
      case ADD:
      case SUB:
      case MUL:
      case DIV:
      case REM:
      case XOR:
        emitBinaryArithmetic(instr, allocation, lines);
        return;
      case ICMP:
        emitCompare(instr, allocation, lines);
        return;
      case FCMP:
        emitFloatCompare(instr, allocation, lines);
        return;
      case FADD:
      case FSUB:
      case FMUL:
      case FDIV:
        emitFloatBinary(instr, allocation, lines);
        return;
      case FNEG:
        materializeInto(lines, instr.getOperands().get(0), "ft0", MachineType.F32, allocation);
        lines.add("  fneg.s ft1, ft0");
        writeDest(lines, instr.getDest(), "ft1", allocation, MachineType.F32);
        return;
      case LOAD:
        emitLoad(instr, allocation, lines);
        return;
      case STORE:
        emitStore(instr, allocation, lines);
        return;
      case MEMZERO:
        emitMemzero(instr, allocation, lines);
        return;
      case BR:
        lines.add("  j " + labelFor(function, ((BlockOperand) instr.getOperands().get(0)).getBlock()));
        return;
      case CONDBR:
        materializeInto(lines, instr.getOperands().get(0), "t0", MachineType.I32, allocation);
        lines.add("  bnez t0, " + labelFor(function, ((BlockOperand) instr.getOperands().get(1)).getBlock()));
        lines.add("  j " + labelFor(function, ((BlockOperand) instr.getOperands().get(2)).getBlock()));
        return;
      case CALL:
        emitCall(instr, allocation, lines);
        return;
      case RET:
        if (!instr.getOperands().isEmpty()) {
          materializeInto(lines, instr.getOperands().get(0), target.getReturnRegister(instr.getType()).getName(), instr.getType(), allocation);
        }
        frameLowering.emitEpilogue(function, lines);
        return;
      default:
        throw new UnsupportedOperationException("Unsupported machine opcode: " + instr.getOpcode());
    }
  }

  private void emitArgIn(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    MachineOperand source = instr.getOperands().get(0);
    if (source instanceof PhysicalRegOperand) {
      String src = ((PhysicalRegOperand) source).getRegister().getName();
      writeDest(lines, instr.getDest(), src, allocation, instr.getType());
      return;
    }
    int stackOffset = (int) ((ImmOperand) source).getValue();
    if (instr.getType().isFloat()) {
      frameLowering.emitLoadFromBase(lines, "ft0", "s0", stackOffset, "t3", MachineType.F32);
      writeDest(lines, instr.getDest(), "ft0", allocation, instr.getType());
    } else {
      frameLowering.emitLoadFromBase(lines, "t0", "s0", stackOffset, "t3", instr.getType());
      writeDest(lines, instr.getDest(), "t0", allocation, instr.getType());
    }
  }

  private void emitBinaryArithmetic(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    ensureIntType(instr.getType());
    MachineOpcode opcode = instr.getOpcode();
    boolean wordResult = instr.getType() == MachineType.I32;
    MachineOperand lhs = instr.getOperands().get(0);
    MachineOperand rhs = instr.getOperands().get(1);
    if ((opcode == MachineOpcode.ADD || opcode == MachineOpcode.XOR)
        && lhs instanceof ImmOperand && !(rhs instanceof ImmOperand)) {
      MachineOperand temporary = lhs;
      lhs = rhs;
      rhs = temporary;
    }
    materializeInto(lines, lhs, "t0", inferOperandType(lhs), allocation);
    if (rhs instanceof ImmOperand immediate) {
      long value = immediate.getValue();
      String immediateOpcode = switch (opcode) {
        case ADD -> fitsSigned12(value) ? wordResult ? "addiw" : "addi" : null;
        case SUB -> value != Long.MIN_VALUE && fitsSigned12(-value)
            ? wordResult ? "addiw" : "addi"
            : null;
        case XOR -> fitsSigned12(value) ? "xori" : null;
        default -> null;
      };
      if (immediateOpcode != null) {
        long encodedValue = opcode == MachineOpcode.SUB ? -value : value;
        lines.add("  " + immediateOpcode + " t2, t0, " + encodedValue);
        if (wordResult && opcode == MachineOpcode.XOR) {
          lines.add("  sext.w t2, t2");
        }
        writeDest(lines, instr.getDest(), "t2", allocation, instr.getType());
        return;
      }
    }
    materializeInto(lines, rhs, "t1", inferOperandType(rhs), allocation);
    String op = switch (opcode) {
      case ADD -> wordResult ? "addw" : "add";
      case SUB -> wordResult ? "subw" : "sub";
      case MUL -> wordResult ? "mulw" : "mul";
      case DIV -> wordResult ? "divw" : "div";
      case REM -> wordResult ? "remw" : "rem";
      case XOR -> "xor";
      default -> throw new IllegalStateException("Unsupported arithmetic opcode: " + opcode);
    };
    lines.add("  " + op + " t2, t0, t1");
    if (wordResult && opcode == MachineOpcode.XOR) {
      lines.add("  sext.w t2, t2");
    }
    writeDest(lines, instr.getDest(), "t2", allocation, instr.getType());
  }

  private static boolean fitsSigned12(long value) {
    return value >= -2048 && value <= 2047;
  }

  private void emitCompare(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    materializeInto(lines, instr.getOperands().get(0), "t0", MachineType.I32, allocation);
    // RISC-V's zero register avoids materializing the constant.
    //   icmp eq  x, 0 -> seqz t2, t0
    //   icmp slt x, 0 -> slt  t2, t0, zero
    //   icmp sle x, 0 -> slt  t2, zero, t0; xori t2, t2, 1
    MachineOperand rightOperand = instr.getOperands().get(1);
    boolean comparesWithZero =
        rightOperand instanceof ImmOperand immediate && immediate.getValue() == 0;
    String right = comparesWithZero ? "zero" : "t1";
    if (!comparesWithZero) {
      materializeInto(lines, rightOperand, right, MachineType.I32, allocation);
    }

    String predicate = instr.getPredicate();
    switch (predicate) {
      case "eq", "ne" -> {
        String source = "t0";
        if (!comparesWithZero) {
          lines.add("  sub t2, t0, " + right);
          source = "t2";
        }
        lines.add("  " + (predicate.equals("eq") ? "seqz" : "snez") + " t2, " + source);
      }
      case "slt" -> lines.add("  slt t2, t0, " + right);
      case "sgt" -> lines.add("  slt t2, " + right + ", t0");
      case "sle" -> {
        lines.add("  slt t2, " + right + ", t0");
        lines.add("  xori t2, t2, 1");
      }
      case "sge" -> {
        lines.add("  slt t2, t0, " + right);
        lines.add("  xori t2, t2, 1");
      }
      default -> throw new UnsupportedOperationException(
          "Unsupported integer compare predicate: " + predicate);
    }
    writeDest(lines, instr.getDest(), "t2", allocation, MachineType.I1);
  }

  private void emitLoad(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    materializeInto(lines, instr.getOperands().get(0), "t0", MachineType.PTR, allocation);
    if (instr.getType().isFloat()) {
      lines.add("  flw ft0, 0(t0)");
      writeDest(lines, instr.getDest(), "ft0", allocation, MachineType.F32);
    } else {
      lines.add("  " + frameLowering.loadMnemonic(instr.getType()) + " t1, 0(t0)");
      writeDest(lines, instr.getDest(), "t1", allocation, instr.getType());
    }
  }

  private void emitStore(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    materializeInto(lines, instr.getOperands().get(0), instr.getType().isFloat() ? "ft0" : "t0", instr.getType(), allocation);
    materializeInto(lines, instr.getOperands().get(1), "t1", MachineType.PTR, allocation);
    if (instr.getType().isFloat()) {
      lines.add("  fsw ft0, 0(t1)");
    } else {
      lines.add("  " + frameLowering.storeMnemonic(instr.getType()) + " t0, 0(t1)");
    }
  }

  private void emitMemzero(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    materializeInto(lines, instr.getOperands().get(0), "t0", MachineType.PTR, allocation);
    int size = (int) ((ImmOperand) instr.getOperands().get(1)).getValue();
    for (int offset = 0; offset < size; offset += 4) {
      frameLowering.emitStoreToBase(lines, "zero", "t0", offset, "t3", MachineType.I32);
    }
  }

  private void emitCall(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    RISCVTarget.CallArgCursor argCursor = target.newCallArgCursor();
    for (int i = 0; i < instr.getOperands().size(); i++) {
      MachineOperand operand = instr.getOperands().get(i);
      MachineType argType = inferOperandType(operand);
      RISCVTarget.CallArgAssignment assignment = target.assignCallArg(argCursor, argType);
      if (assignment.isInRegister()) {
        materializeInto(lines, operand, assignment.getRegister().getName(), argType, allocation);
      } else if (argType.isFloat()) {
          materializeInto(lines, operand, "ft0", argType, allocation);
          frameLowering.emitStoreToBase(lines, "ft0", "sp", assignment.getStackOffset(), "t3", MachineType.F32);
      } else {
        materializeInto(lines, operand, "t0", argType, allocation);
        frameLowering.emitStoreToBase(lines, "t0", "sp", assignment.getStackOffset(), "t3", argType);
      }
    }
    lines.add("  call " + instr.getCallee());
    if (instr.getDest() != null) {
      writeDest(lines, instr.getDest(), target.getReturnRegister(instr.getType()).getName(), allocation, instr.getType());
    }
  }

  private void materializeInto(
      List<String> lines,
      MachineOperand operand,
      String dstReg,
      MachineType type,
      AllocationResult allocation) {
    if (operand instanceof ImmOperand) {
      if (type.isFloat()) {
        lines.add("  li t3, " + ((ImmOperand) operand).getValue());
        lines.add("  fmv.w.x " + dstReg + ", t3");
      } else {
        lines.add("  li " + dstReg + ", " + ((ImmOperand) operand).getValue());
      }
      return;
    }
    if (operand instanceof FloatImmOperand) {
      int bits = java.lang.Float.floatToRawIntBits(((FloatImmOperand) operand).getValue());
      lines.add("  li t3, " + bits);
      lines.add("  fmv.w.x " + dstReg + ", t3");
      return;
    }
    if (operand instanceof SymbolOperand) {
      lines.add("  la " + dstReg + ", " + ((SymbolOperand) operand).getSymbol());
      return;
    }
    if (operand instanceof VRegOperand) {
      ValueLocation location = allocation.locationOf(((VRegOperand) operand).getRegister());
      if (location == null) {
        throw new IllegalStateException("Missing allocation for " + ((VRegOperand) operand).getRegister());
      }
      if (location.isRegister()) {
        String src = ((RegisterLocation) location).getRegister().getName();
        if (!src.equals(dstReg)) {
          if (type.isFloat()) {
            lines.add("  fsgnj.s " + dstReg + ", " + src + ", " + src);
          } else {
            lines.add("  mv " + dstReg + ", " + src);
          }
        }
      } else {
        frameLowering.emitLoadFromBase(lines, dstReg, "sp", ((StackLocation) location).getSlot().getOffset(), "t3", type);
      }
      return;
    }
    throw new UnsupportedOperationException("Cannot materialize operand kind " + operand.getKind());
  }

  private void writeDest(
      List<String> lines,
      VirtualRegister dest,
      String srcReg,
      AllocationResult allocation,
      MachineType type) {
    if (dest == null) return;
    ValueLocation location = allocation.locationOf(dest);
    if (location == null) throw new IllegalStateException("Missing allocation for " + dest);
    if (location.isRegister()) {
      String dstReg = ((RegisterLocation) location).getRegister().getName();
      if (!dstReg.equals(srcReg)) {
        if (type.isFloat()) {
          lines.add("  fsgnj.s " + dstReg + ", " + srcReg + ", " + srcReg);
        } else {
          lines.add("  mv " + dstReg + ", " + srcReg);
        }
      }
      return;
    }
    frameLowering.emitStoreToBase(lines, srcReg, "sp", ((StackLocation) location).getSlot().getOffset(), "t3", type);
  }

  private void emitFloatBinary(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    materializeInto(lines, instr.getOperands().get(0), "ft0", MachineType.F32, allocation);
    materializeInto(lines, instr.getOperands().get(1), "ft1", MachineType.F32, allocation);
    String op;
    switch (instr.getOpcode()) {
      case FADD:
        op = "fadd.s";
        break;
      case FSUB:
        op = "fsub.s";
        break;
      case FMUL:
        op = "fmul.s";
        break;
      case FDIV:
        op = "fdiv.s";
        break;
      default:
        throw new IllegalStateException();
    }
    lines.add("  " + op + " ft2, ft0, ft1");
    writeDest(lines, instr.getDest(), "ft2", allocation, MachineType.F32);
  }

  private void emitFloatCompare(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    materializeInto(lines, instr.getOperands().get(0), "ft0", MachineType.F32, allocation);
    materializeInto(lines, instr.getOperands().get(1), "ft1", MachineType.F32, allocation);
    switch (instr.getPredicate()) {
      case "oeq":
        lines.add("  feq.s t2, ft0, ft1");
        break;
      case "one":
        lines.add("  feq.s t2, ft0, ft1");
        lines.add("  seqz t2, t2");
        break;
      case "une":
        lines.add("  feq.s t2, ft0, ft1");
        lines.add("  seqz t2, t2");
        break;
      case "olt":
        lines.add("  flt.s t2, ft0, ft1");
        break;
      case "ogt":
        lines.add("  flt.s t2, ft1, ft0");
        break;
      case "ole":
        lines.add("  fle.s t2, ft0, ft1");
        break;
      case "oge":
        lines.add("  fle.s t2, ft1, ft0");
        break;
      default:
        throw new UnsupportedOperationException("Unsupported float compare predicate: " + instr.getPredicate());
    }
    writeDest(lines, instr.getDest(), "t2", allocation, MachineType.I1);
  }

  private MachineType inferOperandType(MachineOperand operand) {
    if (operand instanceof VRegOperand) return ((VRegOperand) operand).getRegister().getType();
    if (operand instanceof FloatImmOperand) return MachineType.F32;
    if (operand instanceof SymbolOperand) return MachineType.PTR;
    if (operand instanceof StackSlotOperand) return ((StackSlotOperand) operand).getSlot().getType();
    if (operand instanceof ImmOperand) return MachineType.I32;
    return MachineType.I32;
  }

  private String labelFor(MachineFunction function, MachineBasicBlock block) {
    return ".L_" + function.getName() + "_" + block.getLabel().replace('.', '_');
  }

  private void ensureIntType(MachineType type) {
    if (type.isFloat()) {
      throw new UnsupportedOperationException("Float backend is not implemented yet");
    }
  }
}
