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

public final class RISCVAllocationRewriter {
  private final RISCVTarget target;
  private final RISCVFrameLowering frameLowering;

  public RISCVAllocationRewriter(RISCVTarget target, RISCVFrameLowering frameLowering) {
    this.target = target;
    this.frameLowering = frameLowering;
  }

  void emitInstruction(
      MachineFunction function,
      MachineBasicBlock fallthrough,
      MachineInstr instr,
      AllocationResult allocation,
      List<String> lines) {
    switch (instr.getOpcode()) {
      case ARG_IN:
        emitArgIn(function, instr, allocation, lines);
        return;
      case CONST_INT:
        String constantDest = destinationRegister(instr.getDest(), "t0", allocation);
        materializeInto(
            lines, instr.getOperands().get(0), constantDest, instr.getType(), allocation);
        writeDest(lines, instr.getDest(), constantDest, allocation, instr.getType());
        return;
      case STACK_ADDR:
        StackSlot slot = ((StackSlotOperand) instr.getOperands().get(0)).getSlot();
        String addressDest = destinationRegister(instr.getDest(), "t0", allocation);
        frameLowering.emitAddImmediate(
            lines, addressDest, "sp", slot.getOffset(), "t3");
        writeDest(lines, instr.getDest(), addressDest, allocation, instr.getType());
        return;
      case MOVE:
        if (isRedundantMove(instr, allocation)) return;
        String moveDest = destinationRegister(
            instr.getDest(), instr.getType().isFloat() ? "ft0" : "t0", allocation);
        materializeInto(
            lines,
            instr.getOperands().get(0),
            moveDest,
            inferOperandType(instr.getOperands().get(0)),
            allocation);
        writeDest(lines, instr.getDest(), moveDest, allocation, instr.getType());
        return;
      case ZEXT:
      case SEXT:
        String extendDest = destinationRegister(instr.getDest(), "t0", allocation);
        materializeInto(lines, instr.getOperands().get(0), extendDest,
            inferOperandType(instr.getOperands().get(0)), allocation);
        writeDest(lines, instr.getDest(), extendDest, allocation, instr.getType());
        return;
      case SITOFP:
        String integerSource = readRegister(
            lines, instr.getOperands().get(0), "t0", MachineType.I32, allocation);
        String floatDest = destinationRegister(instr.getDest(), "ft0", allocation);
        lines.add("  fcvt.s.w " + floatDest + ", " + integerSource);
        writeDest(lines, instr.getDest(), floatDest, allocation, MachineType.F32);
        return;
      case FPTOSI:
        String floatSource = readRegister(
            lines, instr.getOperands().get(0), "ft0", MachineType.F32, allocation);
        String integerDest = destinationRegister(instr.getDest(), "t0", allocation);
        lines.add("  fcvt.w.s " + integerDest + ", " + floatSource + ", rtz");
        writeDest(lines, instr.getDest(), integerDest, allocation, MachineType.I32);
        return;
      case ADD:
      case SUB:
      case MUL:
      case DIV:
      case REM:
      case XOR:
      case AND:
      case SHL:
      case ASHR:
      case LSHR:
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
        String negateSource = readRegister(
            lines, instr.getOperands().get(0), "ft0", MachineType.F32, allocation);
        String negateDest = destinationRegister(instr.getDest(), "ft1", allocation);
        lines.add("  fneg.s " + negateDest + ", " + negateSource);
        writeDest(lines, instr.getDest(), negateDest, allocation, MachineType.F32);
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
        MachineBasicBlock branchTarget = ((BlockOperand) instr.getOperands().get(0)).getBlock();
        if (branchTarget != fallthrough) {
          lines.add("  j " + labelFor(function, branchTarget));
        }
        return;
      case CONDBR:
        if (instr.getPredicate() != null) {
          emitCompareBranch(function, fallthrough, instr, allocation, lines);
          return;
        }
        String condition = readRegister(
            lines, instr.getOperands().get(0), "t0", MachineType.I32, allocation);
        MachineBasicBlock ifTrue = ((BlockOperand) instr.getOperands().get(1)).getBlock();
        MachineBasicBlock ifFalse = ((BlockOperand) instr.getOperands().get(2)).getBlock();
        boolean invert = ifTrue == fallthrough;
        lines.add("  " + (invert ? "beqz " : "bnez ") + condition + ", "
            + labelFor(function, invert ? ifFalse : ifTrue));
        if (ifTrue != fallthrough && ifFalse != fallthrough) {
          lines.add("  j " + labelFor(function, ifFalse));
        }
        return;
      case CALL:
        emitCall(instr, allocation, lines);
        return;
      case TAILCALL:
        emitTailCall(function, instr, allocation, lines);
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

  private void emitCompareBranch(
      MachineFunction function,
      MachineBasicBlock fallthrough,
      MachineInstr branch,
      AllocationResult allocation,
      List<String> lines) {
    MachineBasicBlock ifTrue = ((BlockOperand) branch.getOperands().get(1)).getBlock();
    MachineBasicBlock ifFalse = ((BlockOperand) branch.getOperands().get(2)).getBlock();
    boolean invert = ifTrue == fallthrough;
    emitCompareBranch(
        function, invert ? invertPredicate(branch.getPredicate()) : branch.getPredicate(),
        branch.getOperands().get(0), branch.getOperands().get(3),
        invert ? ifFalse : ifTrue, allocation, lines);
    if (ifTrue != fallthrough && ifFalse != fallthrough) {
      lines.add("  j " + labelFor(function, ifFalse));
    }
  }

  private void emitCompareBranch(
      MachineFunction function,
      String predicate,
      MachineOperand leftOperand,
      MachineOperand rightOperand,
      MachineBasicBlock destination,
      AllocationResult allocation,
      List<String> lines) {
    String left = readRegister(lines, leftOperand, "t0", MachineType.I32, allocation);
    String right = "t1";
    if (rightOperand instanceof ImmOperand immediate && immediate.getValue() == 0) {
      right = "zero";
    } else {
      right = readRegister(lines, rightOperand, right, MachineType.I32, allocation);
    }

    String opcode;
    switch (predicate) {
      case "eq": opcode = "beq"; break;
      case "ne": opcode = "bne"; break;
      case "slt": opcode = "blt"; break;
      case "sge": opcode = "bge"; break;
      case "sgt": opcode = "blt"; break;
      case "sle": opcode = "bge"; break;
      default:
        throw new UnsupportedOperationException(
            "Unsupported integer branch predicate: " + predicate);
    }
    if (predicate.equals("sgt") || predicate.equals("sle")) {
      String temporary = left;
      left = right;
      right = temporary;
    }
    lines.add("  " + opcode + " " + left + ", " + right + ", "
        + labelFor(function, destination));
  }

  private static String invertPredicate(String predicate) {
    return switch (predicate) {
      case "eq" -> "ne";
      case "ne" -> "eq";
      case "slt" -> "sge";
      case "sge" -> "slt";
      case "sgt" -> "sle";
      case "sle" -> "sgt";
      default -> throw new UnsupportedOperationException(
          "Unsupported integer branch predicate: " + predicate);
    };
  }

  private void emitArgIn(
      MachineFunction function, MachineInstr instr,
      AllocationResult allocation, List<String> lines) {
    MachineOperand source = instr.getOperands().get(0);
    if (source instanceof PhysicalRegOperand) {
      String src = ((PhysicalRegOperand) source).getRegister().getName();
      writeDest(lines, instr.getDest(), src, allocation, instr.getType());
      return;
    }
    int stackOffset = (int) ((ImmOperand) source).getValue();
    int incomingOffset = function.getFrameInfo().getFrameSize() + stackOffset;
    if (instr.getType().isFloat()) {
      frameLowering.emitLoadFromBase(lines, "ft0", "sp", incomingOffset, "t3", MachineType.F32);
      writeDest(lines, instr.getDest(), "ft0", allocation, instr.getType());
    } else {
      frameLowering.emitLoadFromBase(lines, "t0", "sp", incomingOffset, "t3", instr.getType());
      writeDest(lines, instr.getDest(), "t0", allocation, instr.getType());
    }
  }

  private void emitBinaryArithmetic(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    ensureIntType(instr.getType());
    boolean wordResult = instr.getType() == MachineType.I32;
    MachineOperand lhs = instr.getOperands().get(0);
    MachineOperand rhs = instr.getOperands().get(1);
    if ((instr.getOpcode() == MachineOpcode.ADD || instr.getOpcode() == MachineOpcode.XOR
        || instr.getOpcode() == MachineOpcode.AND)
        && lhs instanceof ImmOperand && !(rhs instanceof ImmOperand)) {
      MachineOperand temporary = lhs;
      lhs = rhs;
      rhs = temporary;
    }
    String lhsRegister = readRegister(lines, lhs, "t0", inferOperandType(lhs), allocation);
    String destination = destinationRegister(instr.getDest(), "t2", allocation);
    if (rhs instanceof ImmOperand immediate) {
      long value = immediate.getValue();
      if ((instr.getOpcode() == MachineOpcode.SHL
          || instr.getOpcode() == MachineOpcode.ASHR
          || instr.getOpcode() == MachineOpcode.LSHR)
          && value >= 0 && value < (wordResult ? 32 : 64)) {
        String op = switch (instr.getOpcode()) {
          case SHL -> wordResult ? "slliw" : "slli";
          case ASHR -> wordResult ? "sraiw" : "srai";
          case LSHR -> wordResult ? "srliw" : "srli";
          default -> throw new IllegalStateException();
        };
        lines.add("  " + op + " " + destination + ", " + lhsRegister + ", " + value);
        writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
        return;
      }
      if (instr.getOpcode() == MachineOpcode.MUL && value > 0
          && (value & (value - 1)) == 0) {
        int shift = Long.numberOfTrailingZeros(value);
        int maxShift = wordResult ? 31 : 63;
        if (shift <= maxShift) {
          lines.add("  " + (wordResult ? "slliw" : "slli") + " "
              + destination + ", " + lhsRegister + ", " + shift);
          writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
          return;
        }
      }
      if (instr.getOpcode() == MachineOpcode.MUL && value > 0) {
        long lowBit = Long.lowestOneBit(value);
        long otherBit = value - lowBit;
        int maxShift = wordResult ? 31 : 63;
        if (otherBit > 0 && (otherBit & (otherBit - 1)) == 0
            && Long.numberOfTrailingZeros(otherBit) <= maxShift) {
          int firstShift = Long.numberOfTrailingZeros(lowBit);
          int secondShift = Long.numberOfTrailingZeros(otherBit);
          lines.add("  " + (wordResult ? "slliw" : "slli")
              + " t3, " + lhsRegister + ", " + firstShift);
          lines.add("  " + (wordResult ? "slliw" : "slli")
              + " " + destination + ", " + lhsRegister + ", " + secondShift);
          lines.add("  " + (wordResult ? "addw" : "add") + " " + destination
              + ", " + destination + ", t3");
          writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
          return;
        }
        long upperBit = value + lowBit;
        if (upperBit > 0 && (upperBit & (upperBit - 1)) == 0
            && Long.numberOfTrailingZeros(upperBit) <= maxShift) {
          lines.add("  " + (wordResult ? "slliw" : "slli") + " t3, "
              + lhsRegister + ", " + Long.numberOfTrailingZeros(upperBit));
          lines.add("  " + (wordResult ? "slliw" : "slli") + " " + destination
              + ", " + lhsRegister + ", " + Long.numberOfTrailingZeros(lowBit));
          lines.add("  " + (wordResult ? "subw" : "sub") + " " + destination
              + ", t3, " + destination);
          writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
          return;
        }
      }
      if (instr.getOpcode() == MachineOpcode.DIV && wordResult && value > 0
          && (value & (value - 1)) == 0) {
        int shift = Long.numberOfTrailingZeros(value);
        if (shift == 0) {
          lines.add("  addiw " + destination + ", " + lhsRegister + ", 0");
        } else {
          lines.add("  srliw t3, " + lhsRegister + ", " + (32 - shift));
          lines.add("  addw " + destination + ", " + lhsRegister + ", t3");
          lines.add("  sraiw " + destination + ", " + destination + ", " + shift);
        }
        writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
        return;
      }
      if (instr.getOpcode() == MachineOpcode.DIV && wordResult
          && value > 1 && value <= (1L << 30)) {
        emitMagicDivision(lines, lhsRegister, destination, (int) value);
        writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
        return;
      }
      if (instr.getOpcode() == MachineOpcode.REM && wordResult && value > 0
          && (value & (value - 1)) == 0 && value <= (1L << 30)) {
        int shift = Long.numberOfTrailingZeros(value);
        if (shift == 0) {
          lines.add("  li " + destination + ", 0");
        } else {
          long mask = value - 1;
          lines.add("  srliw t3, " + lhsRegister + ", " + (32 - shift));
          lines.add("  addw " + destination + ", " + lhsRegister + ", t3");
          if (fitsSigned12(mask)) {
            lines.add("  andi " + destination + ", " + destination + ", " + mask);
          } else {
            lines.add("  li t1, " + mask);
            lines.add("  and " + destination + ", " + destination + ", t1");
          }
          lines.add("  subw " + destination + ", " + destination + ", t3");
        }
        writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
        return;
      }
      if (instr.getOpcode() == MachineOpcode.REM && wordResult
          && value > 1 && value <= (1L << 30)) {
        if (!lhsRegister.equals("t0")) lines.add("  mv t0, " + lhsRegister);
        emitMagicDivision(lines, "t0", destination, (int) value);
        lines.add("  li t1, " + value);
        lines.add("  mulw t1, " + destination + ", t1");
        lines.add("  subw " + destination + ", t0, t1");
        writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
        return;
      }
      String immediateOpcode = null;
      if (instr.getOpcode() == MachineOpcode.ADD && fitsSigned12(value)) {
        immediateOpcode = wordResult ? "addiw" : "addi";
      } else if (instr.getOpcode() == MachineOpcode.SUB
          && value != Long.MIN_VALUE && fitsSigned12(-value)) {
        immediateOpcode = wordResult ? "addiw" : "addi";
        value = -value;
      } else if (instr.getOpcode() == MachineOpcode.XOR && fitsSigned12(value)) {
        immediateOpcode = "xori";
      } else if (instr.getOpcode() == MachineOpcode.AND && fitsSigned12(value)) {
        immediateOpcode = "andi";
      }
      if (immediateOpcode != null) {
        lines.add("  " + immediateOpcode + " " + destination + ", " + lhsRegister + ", " + value);
        if (wordResult && instr.getOpcode() == MachineOpcode.XOR) {
          lines.add("  sext.w " + destination + ", " + destination);
        }
        writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
        return;
      }
    }
    String rhsRegister = readRegister(lines, rhs, "t1", inferOperandType(rhs), allocation);
    String op;
    switch (instr.getOpcode()) {
      case ADD:
        op = wordResult ? "addw" : "add";
        break;
      case SUB:
        op = wordResult ? "subw" : "sub";
        break;
      case MUL:
        op = wordResult ? "mulw" : "mul";
        break;
      case DIV:
        op = wordResult ? "divw" : "div";
        break;
      case REM:
        op = wordResult ? "remw" : "rem";
        break;
      case XOR:
        op = "xor";
        break;
      case AND:
        op = "and";
        break;
      case SHL:
        op = wordResult ? "sllw" : "sll";
        break;
      case ASHR:
        op = wordResult ? "sraw" : "sra";
        break;
      case LSHR:
        op = wordResult ? "srlw" : "srl";
        break;
      default:
        throw new IllegalStateException();
    }
    lines.add("  " + op + " " + destination + ", " + lhsRegister + ", " + rhsRegister);
    if (wordResult && instr.getOpcode() == MachineOpcode.XOR) {
      lines.add("  sext.w " + destination + ", " + destination);
    }
    writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
  }

  private static boolean fitsSigned12(long value) {
    return value >= -2048 && value <= 2047;
  }

  private static void emitMagicDivision(
      List<String> lines, String numerator, String destination, int divisor) {
    SignedDivisionMagic magic = SignedDivisionMagic.forDivisor(divisor);
    long multiplier = magic.multiplier();
    if (multiplier < (1L << 31)) {
      lines.add("  li t1, " + multiplier);
      lines.add("  mul t1, " + numerator + ", t1");
      lines.add("  srai " + destination + ", t1, " + (32 + magic.postShift()));
    } else {
      lines.add("  li t1, " + (multiplier - (1L << 32)));
      lines.add("  mul t1, " + numerator + ", t1");
      lines.add("  srai t1, t1, 32");
      lines.add("  addw " + destination + ", t1, " + numerator);
      if (magic.postShift() > 0) {
        lines.add("  sraiw " + destination + ", " + destination
            + ", " + magic.postShift());
      }
    }
    lines.add("  sraiw t3, " + numerator + ", 31");
    lines.add("  subw " + destination + ", " + destination + ", t3");
  }

  private void emitCompare(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    String left = readRegister(
        lines, instr.getOperands().get(0), "t0", MachineType.I32, allocation);
    String destination = destinationRegister(instr.getDest(), "t2", allocation);
    if (instr.getOperands().get(1) instanceof ImmOperand immediate && immediate.getValue() == 0) {
      switch (instr.getPredicate()) {
        case "eq":
          lines.add("  seqz " + destination + ", " + left);
          break;
        case "ne":
          lines.add("  snez " + destination + ", " + left);
          break;
        case "slt":
          lines.add("  slt " + destination + ", " + left + ", zero");
          break;
        case "sgt":
          lines.add("  slt " + destination + ", zero, " + left);
          break;
        case "sle":
          lines.add("  slt " + destination + ", zero, " + left);
          lines.add("  xori " + destination + ", " + destination + ", 1");
          break;
        case "sge":
          lines.add("  slt " + destination + ", " + left + ", zero");
          lines.add("  xori " + destination + ", " + destination + ", 1");
          break;
        default:
          throw new UnsupportedOperationException(
              "Unsupported integer compare predicate: " + instr.getPredicate());
      }
      writeDest(lines, instr.getDest(), destination, allocation, MachineType.I1);
      return;
    }
    if (instr.getOperands().get(1) instanceof ImmOperand immediate
        && emitImmediateCompare(
            instr.getPredicate(), immediate.getValue(), left, destination, lines)) {
      writeDest(lines, instr.getDest(), destination, allocation, MachineType.I1);
      return;
    }
    String right = readRegister(
        lines, instr.getOperands().get(1), "t1", MachineType.I32, allocation);
    switch (instr.getPredicate()) {
      case "eq":
        lines.add("  sub " + destination + ", " + left + ", " + right);
        lines.add("  seqz " + destination + ", " + destination);
        break;
      case "ne":
        lines.add("  sub " + destination + ", " + left + ", " + right);
        lines.add("  snez " + destination + ", " + destination);
        break;
      case "slt":
        lines.add("  slt " + destination + ", " + left + ", " + right);
        break;
      case "sgt":
        lines.add("  slt " + destination + ", " + right + ", " + left);
        break;
      case "sle":
        lines.add("  slt " + destination + ", " + right + ", " + left);
        lines.add("  xori " + destination + ", " + destination + ", 1");
        break;
      case "sge":
        lines.add("  slt " + destination + ", " + left + ", " + right);
        lines.add("  xori " + destination + ", " + destination + ", 1");
        break;
      default:
        throw new UnsupportedOperationException("Unsupported integer compare predicate: " + instr.getPredicate());
    }
    writeDest(lines, instr.getDest(), destination, allocation, MachineType.I1);
  }

  private static boolean emitImmediateCompare(
      String predicate, long value, String left, String destination, List<String> lines) {
    switch (predicate) {
      case "eq":
      case "ne":
        if (value == Long.MIN_VALUE || !fitsSigned12(-value)) return false;
        lines.add("  addi " + destination + ", " + left + ", " + -value);
        lines.add("  " + (predicate.equals("eq") ? "seqz" : "snez")
            + " " + destination + ", " + destination);
        return true;
      case "slt":
        if (!fitsSigned12(value)) return false;
        lines.add("  slti " + destination + ", " + left + ", " + value);
        return true;
      case "sge":
        if (!fitsSigned12(value)) return false;
        lines.add("  slti " + destination + ", " + left + ", " + value);
        lines.add("  xori " + destination + ", " + destination + ", 1");
        return true;
      case "sle":
      case "sgt":
        if (value == Long.MAX_VALUE || !fitsSigned12(value + 1)) return false;
        lines.add("  slti " + destination + ", " + left + ", " + (value + 1));
        if (predicate.equals("sgt")) {
          lines.add("  xori " + destination + ", " + destination + ", 1");
        }
        return true;
      default:
        return false;
    }
  }

  private void emitLoad(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    long offset = instr.getOperands().size() > 1
        ? ((ImmOperand) instr.getOperands().get(1)).getValue() : 0;
    String address = readRegister(
        lines, instr.getOperands().get(0), "t0", MachineType.PTR, allocation);
    if (instr.getType().isFloat()) {
      String destination = destinationRegister(instr.getDest(), "ft0", allocation);
      lines.add("  flw " + destination + ", " + offset + "(" + address + ")");
      writeDest(lines, instr.getDest(), destination, allocation, MachineType.F32);
    } else {
      String destination = destinationRegister(instr.getDest(), "t1", allocation);
      lines.add("  " + frameLowering.loadMnemonic(instr.getType())
          + " " + destination + ", " + offset + "(" + address + ")");
      writeDest(lines, instr.getDest(), destination, allocation, instr.getType());
    }
  }

  private void emitStore(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    long offset = instr.getOperands().size() > 2
        ? ((ImmOperand) instr.getOperands().get(2)).getValue() : 0;
    String value = readRegister(
        lines, instr.getOperands().get(0), instr.getType().isFloat() ? "ft0" : "t0",
        instr.getType(), allocation);
    String address = readRegister(
        lines, instr.getOperands().get(1), "t1", MachineType.PTR, allocation);
    if (instr.getType().isFloat()) {
      lines.add("  fsw " + value + ", " + offset + "(" + address + ")");
    } else {
      lines.add("  " + frameLowering.storeMnemonic(instr.getType())
          + " " + value + ", " + offset + "(" + address + ")");
    }
  }

  private void emitMemzero(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    int size = (int) ((ImmOperand) instr.getOperands().get(1)).getValue();
    if (target.shouldUseMemsetLibcall(size)) {
      materializeInto(lines, instr.getOperands().get(0), "a0", MachineType.PTR, allocation);
      lines.add("  li a1, 0");
      lines.add("  li a2, " + size);
      lines.add("  call memset");
      return;
    }
    materializeInto(lines, instr.getOperands().get(0), "t0", MachineType.PTR, allocation);
    for (int offset = 0; offset < size; offset += 4) {
      frameLowering.emitStoreToBase(lines, "zero", "t0", offset, "t3", MachineType.I32);
    }
  }

  private void emitCall(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    emitCallArguments(instr, allocation, lines);
    lines.add("  call " + instr.getCallee());
    if (instr.getDest() != null) {
      writeDest(lines, instr.getDest(), target.getReturnRegister(instr.getType()).getName(), allocation, instr.getType());
    }
  }

  private void emitTailCall(
      MachineFunction function, MachineInstr instr,
      AllocationResult allocation, List<String> lines) {
    emitCallArguments(instr, allocation, lines);
    frameLowering.emitTailEpilogue(function, lines);
    lines.add("  tail " + instr.getCallee());
  }

  private void emitCallArguments(
      MachineInstr instr, AllocationResult allocation, List<String> lines) {
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

  private String readRegister(
      List<String> lines, MachineOperand operand, String fallback, MachineType type,
      AllocationResult allocation) {
    if (operand instanceof VRegOperand virtualRegister) {
      ValueLocation location = allocation.locationOf(virtualRegister.getRegister());
      if (location instanceof RegisterLocation register) {
        return register.getRegister().getName();
      }
    }
    materializeInto(lines, operand, fallback, type, allocation);
    return fallback;
  }

  private static String destinationRegister(
      VirtualRegister destination, String fallback, AllocationResult allocation) {
    ValueLocation location = allocation.locationOf(destination);
    if (location instanceof RegisterLocation register) {
      return register.getRegister().getName();
    }
    return fallback;
  }

  private static boolean isRedundantMove(MachineInstr instruction, AllocationResult allocation) {
    if (!(instruction.getOperands().get(0) instanceof VRegOperand source)) return false;
    ValueLocation sourceLocation = allocation.locationOf(source.getRegister());
    ValueLocation destinationLocation = allocation.locationOf(instruction.getDest());
    if (sourceLocation instanceof StackLocation sourceStack
        && destinationLocation instanceof StackLocation destinationStack) {
      return sourceStack.getSlot() == destinationStack.getSlot();
    }
    if (sourceLocation instanceof RegisterLocation sourceRegister
        && destinationLocation instanceof RegisterLocation destinationRegister) {
      return sourceRegister.getRegister().getName()
          .equals(destinationRegister.getRegister().getName());
    }
    return false;
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
    String destination = destinationRegister(instr.getDest(), "ft2", allocation);
    lines.add("  " + op + " " + destination + ", ft0, ft1");
    writeDest(lines, instr.getDest(), destination, allocation, MachineType.F32);
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
