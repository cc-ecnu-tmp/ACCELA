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
import accela.backend.machine.RVVConfig;
import accela.backend.machine.VectorShape;
import accela.backend.machine.VCIXInfo;
import accela.backend.machine.VectorConstantOperand;
import accela.ir.Constant;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterLocation;
import accela.backend.regalloc.ValueLocation;
import java.util.ArrayList;
import java.util.List;

public final class RISCVAsmEmitter {
  private static final String INT_SCRATCH_0 = "a6";
  private static final String INT_SCRATCH_1 = "a7";
  private static final String ADDRESS_SCRATCH = "a5";
  private static final String FLOAT_SCRATCH_0 = "fa6";
  private static final String FLOAT_SCRATCH_1 = "fa7";

  private final RISCVTarget target;
  private final RISCVFrameLowering frameLowering;
  private final RISCVStrengthReduction strengthReduction =
      new RISCVStrengthReduction(ADDRESS_SCRATCH);
  private int selectLabelCounter;

  public RISCVAsmEmitter(RISCVTarget target, RISCVFrameLowering frameLowering) {
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
      case MOVE:
      case ZEXT:
      case SEXT:
        emitMove(instr, allocation, lines);
        return;
      case SMULH:
        emitSMulH(instr, allocation, lines);
        return;
      case STACK_ADDR:
        emitStackAddress(instr, allocation, lines);
        return;
      case SITOFP:
        lines.add(
            "  fcvt.s.w "
                + destReg(instr, allocation)
                + ", "
                + operandRegisterOrScratch(
                    lines,
                    instr.getOperands().get(0),
                    INT_SCRATCH_0,
                    MachineType.I32,
                    allocation));
        return;
      case FPTOSI:
        lines.add(
            "  fcvt.w.s "
                + destReg(instr, allocation)
                + ", "
                + operandRegisterOrScratch(
                    lines,
                    instr.getOperands().get(0),
                    FLOAT_SCRATCH_0,
                    MachineType.F32,
                    allocation)
                + ", rtz");
        return;
      case ADD:
      case SUB:
      case MUL:
      case DIV:
      case REM:
      case SHL:
      case ASHR:
      case AND:
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
        lines.add(
            "  fneg.s "
                + destReg(instr, allocation)
                + ", "
                + operandRegisterOrScratch(
                    lines,
                    instr.getOperands().get(0),
                    FLOAT_SCRATCH_0,
                    MachineType.F32,
                    allocation));
        return;
      case SELECT:
        emitSelect(function, instr, allocation, lines);
        return;
      case LOAD:
        emitLoad(instr, allocation, lines);
        return;
      case STORE:
        emitStore(instr, allocation, lines);
        return;
      case VSET:
        emitVectorConfiguration(instr, lines);
        return;
      case VLOAD:
        emitVectorLoad(instr, allocation, lines);
        return;
      case VSTORE:
        emitVectorStore(instr, allocation, lines);
        return;
      case VMOVE:
      case VSPLAT:
      case VBUILD:
      case VEXTRACT:
      case VINSERT:
      case VSHUFFLE:
        emitVectorManipulation(instr, allocation, lines);
        return;
      case VADD:
      case VSUB:
      case VMUL:
      case VDIV:
      case VREM:
      case VSHL:
      case VASHR:
      case VAND:
      case VXOR:
      case VFADD:
      case VFSUB:
      case VFMUL:
      case VFDIV:
      case VFNEG:
      case VICMP:
      case VFCMP:
      case VSELECT:
      case VZEXT:
      case VSEXT:
      case VSITOFP:
      case VFPTOSI:
        emitVectorOperation(instr, allocation, lines);
        return;
      case VSMULH:
        throw new UnsupportedOperationException(
            "vector SMULH requires widening legalization before RVV emission");
      case VCIX:
        emitVCIX(instr, allocation, lines);
        return;
      case MEMZERO:
        emitMemzero(instr, allocation, lines);
        return;
      case BR:
        MachineBasicBlock branchTarget = ((BlockOperand) instr.getOperands().get(0)).getBlock();
        if (branchTarget != fallthrough) lines.add("  j " + labelFor(function, branchTarget));
        return;
      case CONDBR:
        if (instr.getPredicate() != null) {
          emitCompareBranch(function, fallthrough, instr, allocation, lines);
          return;
        }
        MachineBasicBlock ifTrue = ((BlockOperand) instr.getOperands().get(1)).getBlock();
        MachineBasicBlock ifFalse = ((BlockOperand) instr.getOperands().get(2)).getBlock();
        boolean invert = ifTrue == fallthrough;
        lines.add(
            "  "
                + (invert ? "beqz " : "bnez ")
                + operandRegisterOrScratch(
                    lines,
                    instr.getOperands().get(0),
                    INT_SCRATCH_0,
                    MachineType.I32,
                    allocation)
                + ", "
                + labelFor(function, invert ? ifFalse : ifTrue));
        if (ifTrue != fallthrough && ifFalse != fallthrough) {
          lines.add("  j " + labelFor(function, ifFalse));
        }
        return;
      case CALL:
        emitCall(instr, allocation, lines);
        return;
      case RET:
        if (!instr.getOperands().isEmpty()) {
          emitMoveToRegister(
              lines,
              instr.getOperands().get(0),
              target.getReturnRegister(instr.getType()).getName(),
              instr.getType(),
              allocation);
        }
        frameLowering.emitEpilogue(function, lines);
        return;
      default:
        throw new UnsupportedOperationException("Unsupported machine opcode: " + instr.getOpcode());
    }
  }

  private void emitSelect(
      MachineFunction function,
      MachineInstr instruction,
      AllocationResult allocation,
      List<String> lines) {
    int id = selectLabelCounter++;
    String falseLabel = ".L_" + function.getName() + "_select_false_" + id;
    String endLabel = ".L_" + function.getName() + "_select_end_" + id;
    String destination = destReg(instruction, allocation);
    String condition = operandRegisterOrScratch(
        lines, instruction.getOperands().get(0), INT_SCRATCH_0, MachineType.I32, allocation);
    MachineOperand ifTrue = instruction.getOperands().get(1);
    MachineOperand ifFalse = instruction.getOperands().get(2);
    if (isRegister(ifFalse) && sourceReg(ifFalse, allocation).equals(destination)) {
      lines.add("  beqz " + condition + ", " + endLabel);
      emitMoveToRegister(lines, ifTrue, destination, instruction.getType(), allocation);
      lines.add(endLabel + ":");
      return;
    }
    if (isRegister(ifTrue) && sourceReg(ifTrue, allocation).equals(destination)) {
      lines.add("  bnez " + condition + ", " + endLabel);
      emitMoveToRegister(lines, ifFalse, destination, instruction.getType(), allocation);
      lines.add(endLabel + ":");
      return;
    }
    lines.add("  beqz " + condition + ", " + falseLabel);
    emitMoveToRegister(lines, ifTrue, destination, instruction.getType(), allocation);
    lines.add("  j " + endLabel);
    lines.add(falseLabel + ":");
    emitMoveToRegister(
        lines,
        ifFalse,
        destination,
        instruction.getType(),
        allocation);
    lines.add(endLabel + ":");
  }

  private static boolean isRegister(MachineOperand operand) {
    return operand instanceof VRegOperand || operand instanceof PhysicalRegOperand;
  }

  private void emitCompareBranch(
      MachineFunction function,
      MachineBasicBlock fallthrough,
      MachineInstr branch,
      AllocationResult allocation,
      List<String> lines) {
    MachineBasicBlock ifTrue = ((BlockOperand) branch.getOperands().get(2)).getBlock();
    MachineBasicBlock ifFalse = ((BlockOperand) branch.getOperands().get(3)).getBlock();
    boolean invert = ifTrue == fallthrough;
    String predicate = invert ? invertPredicate(branch.getPredicate()) : branch.getPredicate();
    String left =
        operandRegisterOrScratch(
            lines,
            branch.getOperands().get(0),
            INT_SCRATCH_0,
            MachineType.I32,
            allocation);
    MachineOperand rightOperand = branch.getOperands().get(1);
    String right;
    if (rightOperand instanceof ImmOperand immediate && immediate.getValue() == 0) {
      right = "zero";
    } else {
      right =
          operandRegisterOrScratch(
              lines, rightOperand, INT_SCRATCH_1, MachineType.I32, allocation);
    }

    String comparison = switch (predicate) {
      case "eq" -> "beq " + left + ", " + right;
      case "ne" -> "bne " + left + ", " + right;
      case "slt" -> "blt " + left + ", " + right;
      case "sge" -> "bge " + left + ", " + right;
      case "sgt" -> "blt " + right + ", " + left;
      case "sle" -> "bge " + right + ", " + left;
      default -> throw new UnsupportedOperationException(
          "Unsupported integer branch predicate: " + predicate);
    };
    lines.add("  " + comparison + ", " + labelFor(function, invert ? ifFalse : ifTrue));
    if (ifTrue != fallthrough && ifFalse != fallthrough) {
      lines.add("  j " + labelFor(function, ifFalse));
    }
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
      MachineFunction function,
      MachineInstr instr,
      AllocationResult allocation,
      List<String> lines) {
    MachineOperand source = instr.getOperands().get(0);
    String dst = destReg(instr, allocation);
    if (source instanceof PhysicalRegOperand) {
      PhysicalRegister sourceRegister = ((PhysicalRegOperand) source).getRegister();
      if (instr.getType().isFloat() && !sourceRegister.getType().isFloat()) {
        lines.add("  fmv.w.x " + dst + ", " + sourceRegister.getName());
      } else {
        emitRegisterMove(lines, dst, sourceRegister.getName(), instr.getType());
      }
      return;
    }

    // The prologue moves sp down by frameSize; ABI stack offsets start at the caller's sp.
    int incomingOffset =
        function.getFrameInfo().getFrameSize() + (int) ((ImmOperand) source).getValue();
    frameLowering.emitLoadFromBase(
        lines, dst, "sp", incomingOffset, ADDRESS_SCRATCH, instr.getType());
  }

  private void emitMove(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    if (instr.getType().isVector()
        && (instr.getOperands().get(0) instanceof VRegOperand
            || instr.getOperands().get(0) instanceof PhysicalRegOperand)) {
      emitVectorRegisterMove(
          lines,
          destReg(instr, allocation),
          sourceReg(instr.getOperands().get(0), allocation),
          instr.getDest().getVectorShape());
      return;
    }
    emitMoveToRegister(
        lines,
        instr.getOperands().get(0),
        destReg(instr, allocation),
        inferOperandType(instr.getOperands().get(0)),
        allocation);
  }

  private void emitStackAddress(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    StackSlot slot = ((StackSlotOperand) instr.getOperands().get(0)).getSlot();
    frameLowering.emitAddImmediate(
        lines, destReg(instr, allocation), "sp", slot.getOffset(), ADDRESS_SCRATCH);
  }

  private void emitBinaryArithmetic(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    ensureIntType(instr.getType());
    MachineOpcode opcode = instr.getOpcode();
    boolean wordResult = instr.getType() == MachineType.I32;
    MachineOperand lhs = instr.getOperands().get(0);
    MachineOperand rhs = instr.getOperands().get(1);
    if (opcode == MachineOpcode.SUB
        && lhs instanceof ImmOperand immediate
        && immediate.getValue() == 0
        && !(rhs instanceof ImmOperand)) {
      String rhsReg =
          operandRegisterOrScratch(
              lines, rhs, INT_SCRATCH_0, inferOperandType(rhs), allocation);
      lines.add("  " + (wordResult ? "negw" : "neg") + " "
          + destReg(instr, allocation) + ", " + rhsReg);
      return;
    }
    if ((opcode == MachineOpcode.ADD || opcode == MachineOpcode.MUL
            || opcode == MachineOpcode.AND || opcode == MachineOpcode.XOR)
        && lhs instanceof ImmOperand && !(rhs instanceof ImmOperand)) {
      MachineOperand temporary = lhs;
      lhs = rhs;
      rhs = temporary;
    }
    String lhsReg =
        operandRegisterOrScratch(
            lines, lhs, INT_SCRATCH_0, inferOperandType(lhs), allocation);
    String dst = destReg(instr, allocation);
    if (rhs instanceof ImmOperand immediate) {
      long value = immediate.getValue();
      if (strengthReduction.emit(opcode, value, lhsReg, dst, wordResult, lines)) return;
      String immediateOpcode = switch (opcode) {
        case ADD -> fitsSigned12(value) ? wordResult ? "addiw" : "addi" : null;
        case SUB -> value != Long.MIN_VALUE && fitsSigned12(-value)
            ? wordResult ? "addiw" : "addi"
            : null;
        case XOR -> fitsSigned12(value) ? "xori" : null;
        case AND -> fitsSigned12(value) ? "andi" : null;
        case SHL -> value >= 0 && value < (wordResult ? Integer.SIZE : Long.SIZE)
            ? wordResult ? "slliw" : "slli"
            : null;
        case ASHR -> value >= 0 && value < (wordResult ? Integer.SIZE : Long.SIZE)
            ? wordResult ? "sraiw" : "srai"
            : null;
        default -> null;
      };
      if (immediateOpcode != null) {
        long encodedValue = opcode == MachineOpcode.SUB ? -value : value;
        lines.add("  " + immediateOpcode + " " + dst + ", " + lhsReg + ", " + encodedValue);
        if (wordResult && opcode == MachineOpcode.XOR) {
          lines.add("  sext.w " + dst + ", " + dst);
        }
        return;
      }
    }
    String rhsReg =
        operandRegisterOrScratch(
            lines, rhs, INT_SCRATCH_1, inferOperandType(rhs), allocation);
    String op = switch (opcode) {
      case ADD -> wordResult ? "addw" : "add";
      case SUB -> wordResult ? "subw" : "sub";
      case MUL -> wordResult ? "mulw" : "mul";
      case DIV -> wordResult ? "divw" : "div";
      case REM -> wordResult ? "remw" : "rem";
      case SHL -> wordResult ? "sllw" : "sll";
      case ASHR -> wordResult ? "sraw" : "sra";
      case AND -> "and";
      case XOR -> "xor";
      default -> throw new IllegalStateException("Unsupported arithmetic opcode: " + opcode);
    };
    lines.add("  " + op + " " + dst + ", " + lhsReg + ", " + rhsReg);
    if (wordResult && opcode == MachineOpcode.XOR) {
      lines.add("  sext.w " + dst + ", " + dst);
    }
  }

  private void emitSMulH(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    String lhs = operandRegisterOrScratch(
        lines, instruction.getOperands().get(0), INT_SCRATCH_0, MachineType.I32, allocation);
    String rhs = operandRegisterOrScratch(
        lines, instruction.getOperands().get(1), INT_SCRATCH_1, MachineType.I32, allocation);
    String dst = destReg(instruction, allocation);
    long extraShift = instruction.getOperands().size() == 3
        ? ((ImmOperand) instruction.getOperands().get(2)).getValue()
        : 0;
    lines.add("  mul " + ADDRESS_SCRATCH + ", " + lhs + ", " + rhs);
    lines.add("  srai " + dst + ", " + ADDRESS_SCRATCH + ", " + (Integer.SIZE + extraShift));
  }

  private static boolean fitsSigned12(long value) {
    return value >= -2048 && value <= 2047;
  }

  private void emitCompare(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    String dst = destReg(instr, allocation);
    String lhs =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(0),
            INT_SCRATCH_0,
            MachineType.I32,
            allocation);
    MachineOperand rightOperand = instr.getOperands().get(1);
    boolean comparesWithZero =
        rightOperand instanceof ImmOperand immediate && immediate.getValue() == 0;
    if (!comparesWithZero
        && rightOperand instanceof ImmOperand immediate
        && emitImmediateCompare(instr.getPredicate(), immediate.getValue(), lhs, dst, lines)) {
      return;
    }
    String right =
        comparesWithZero
            ? "zero"
            : operandRegisterOrScratch(
                lines, rightOperand, INT_SCRATCH_1, MachineType.I32, allocation);

    String predicate = instr.getPredicate();
    switch (predicate) {
      case "eq", "ne" -> {
        String source = lhs;
        if (!comparesWithZero) {
          lines.add("  sub " + dst + ", " + lhs + ", " + right);
          source = dst;
        }
        lines.add("  " + (predicate.equals("eq") ? "seqz" : "snez") + " " + dst + ", " + source);
      }
      case "slt" -> lines.add("  slt " + dst + ", " + lhs + ", " + right);
      case "sgt" -> lines.add("  slt " + dst + ", " + right + ", " + lhs);
      case "sle" -> {
        lines.add("  slt " + dst + ", " + right + ", " + lhs);
        lines.add("  xori " + dst + ", " + dst + ", 1");
      }
      case "sge" -> {
        lines.add("  slt " + dst + ", " + lhs + ", " + right);
        lines.add("  xori " + dst + ", " + dst + ", 1");
      }
      default -> throw new UnsupportedOperationException(
          "Unsupported integer compare predicate: " + predicate);
    }
  }

  private static boolean emitImmediateCompare(
      String predicate, long value, String lhs, String dst, List<String> lines) {
    switch (predicate) {
      case "eq", "ne" -> {
        if (value == Long.MIN_VALUE || !fitsSigned12(-value)) return false;
        lines.add("  addi " + dst + ", " + lhs + ", " + -value);
        lines.add("  " + (predicate.equals("eq") ? "seqz" : "snez") + " " + dst + ", " + dst);
        return true;
      }
      case "slt", "sge" -> {
        if (!fitsSigned12(value)) return false;
        lines.add("  slti " + dst + ", " + lhs + ", " + value);
        if (predicate.equals("sge")) lines.add("  xori " + dst + ", " + dst + ", 1");
        return true;
      }
      case "sle", "sgt" -> {
        if (value == Long.MAX_VALUE || !fitsSigned12(value + 1)) return false;
        lines.add("  slti " + dst + ", " + lhs + ", " + (value + 1));
        if (predicate.equals("sgt")) lines.add("  xori " + dst + ", " + dst + ", 1");
        return true;
      }
      default -> {
        return false;
      }
    }
  }

  private void emitVectorConfiguration(MachineInstr instruction, List<String> lines) {
    RVVConfig config = instruction.getRVVConfig();
    if (config == null) throw new IllegalArgumentException("VSET has no vector configuration");
    if (config.avl() <= 31) {
      lines.add(
          "  vsetivli zero, " + config.avl() + ", " + config.vtypeAssembly());
    } else {
      lines.add("  li " + INT_SCRATCH_0 + ", " + config.avl());
      lines.add(
          "  vsetvli zero, " + INT_SCRATCH_0 + ", " + config.vtypeAssembly());
    }
  }

  private void emitVectorLoad(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    VectorShape shape = instruction.getDest().getVectorShape();
    String address =
        operandRegisterOrScratch(
            lines,
            instruction.getOperands().get(0),
            INT_SCRATCH_0,
            MachineType.PTR,
            allocation);
    address = vectorAddressWithOffset(instruction, 1, address, lines);
    String mnemonic = shape.mask() ? "vlm.v" : "vle" + shape.sew() + ".v";
    lines.add("  " + mnemonic + " " + destReg(instruction, allocation) + ", (" + address + ")");
  }

  private void emitVectorStore(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    VirtualRegister valueRegister =
        ((VRegOperand) instruction.getOperands().get(0)).getRegister();
    VectorShape shape = valueRegister.getVectorShape();
    String address =
        operandRegisterOrScratch(
            lines,
            instruction.getOperands().get(1),
            INT_SCRATCH_0,
            MachineType.PTR,
            allocation);
    address = vectorAddressWithOffset(instruction, 2, address, lines);
    String mnemonic = shape.mask() ? "vsm.v" : "vse" + shape.sew() + ".v";
    lines.add(
        "  " + mnemonic + " " + sourceReg(instruction.getOperands().get(0), allocation)
            + ", (" + address + ")");
  }

  private String vectorAddressWithOffset(
      MachineInstr instruction, int offsetIndex, String address, List<String> lines) {
    if (instruction.getOperands().size() <= offsetIndex) return address;
    long offset = ((ImmOperand) instruction.getOperands().get(offsetIndex)).getValue();
    if (offset == 0) return address;
    frameLowering.emitAddImmediate(
        lines, INT_SCRATCH_1, address, (int) offset, ADDRESS_SCRATCH);
    return INT_SCRATCH_1;
  }

  private void emitVectorManipulation(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    switch (instruction.getOpcode()) {
      case VMOVE -> emitVectorRegisterMove(
          lines,
          destReg(instruction, allocation),
          sourceReg(instruction.getOperands().get(0), allocation),
          instruction.getDest().getVectorShape());
      case VSPLAT -> emitVectorSplat(instruction, allocation, lines);
      case VBUILD -> emitVectorBuild(instruction, allocation, lines);
      case VEXTRACT -> emitVectorExtract(instruction, allocation, lines);
      case VINSERT -> emitVectorInsert(instruction, allocation, lines);
      case VSHUFFLE -> emitVectorShuffle(instruction, allocation, lines);
      default -> throw new IllegalStateException("not a vector manipulation opcode");
    }
  }

  private void emitVectorSplat(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    VectorShape shape = instruction.getDest().getVectorShape();
    String destination = destReg(instruction, allocation);
    MachineOperand value = instruction.getOperands().get(0);
    if (!shape.elementType().isFloat() && value instanceof ImmOperand immediate
        && immediate.getValue() >= -16 && immediate.getValue() <= 15) {
      lines.add("  vmv.v.i " + destination + ", " + immediate.getValue());
      return;
    }
    String source =
        operandRegisterOrScratch(
            lines,
            value,
            shape.elementType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0,
            shape.elementType(),
            allocation);
    lines.add(
        "  " + (shape.elementType().isFloat() ? "vfmv.v.f " : "vmv.v.x ")
            + destination + ", " + source);
  }

  private void emitVectorBuild(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    VectorShape shape = instruction.getDest().getVectorShape();
    String destination = destReg(instruction, allocation);
    if (!shape.elementType().isFloat()
        && emitIntegerSequenceBuild(instruction, destination, lines)) return;
    for (int lane = 0; lane < instruction.getOperands().size(); lane++) {
      MachineOperand value = instruction.getOperands().get(lane);
      String source =
          operandRegisterOrScratch(
              lines,
              value,
              shape.elementType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0,
              shape.elementType(),
              allocation);
      String insertDestination = lane == 0 ? destination : "v0";
      lines.add(
          "  " + (shape.elementType().isFloat() ? "vfmv.s.f " : "vmv.s.x ")
              + insertDestination + ", " + source);
      if (lane == 0) {
        continue;
      } else if (lane <= 31) {
        lines.add("  vslideup.vi " + destination + ", v0, " + lane);
      } else {
        lines.add("  li " + INT_SCRATCH_1 + ", " + lane);
        lines.add("  vslideup.vx " + destination + ", v0, " + INT_SCRATCH_1);
      }
    }
  }

  private boolean emitIntegerSequenceBuild(
      MachineInstr instruction, String destination, List<String> lines) {
    if (instruction.getOperands().isEmpty()
        || instruction.getOperands().stream().anyMatch(operand -> !(operand instanceof ImmOperand))) {
      return false;
    }
    long first = ((ImmOperand) instruction.getOperands().get(0)).getValue();
    long step =
        instruction.getOperands().size() == 1
            ? 0
            : ((ImmOperand) instruction.getOperands().get(1)).getValue() - first;
    for (int lane = 1; lane < instruction.getOperands().size(); lane++) {
      long expected = first + step * lane;
      if (((ImmOperand) instruction.getOperands().get(lane)).getValue() != expected) return false;
    }
    if (step == 0) {
      emitIntegerVectorSplat(destination, first, lines);
      return true;
    }
    lines.add("  vid.v " + destination);
    if (step != 1) {
      lines.add("  li " + INT_SCRATCH_0 + ", " + step);
      lines.add("  vmul.vx " + destination + ", " + destination + ", " + INT_SCRATCH_0);
    }
    if (first >= -16 && first <= 15) {
      if (first != 0) {
        lines.add("  vadd.vi " + destination + ", " + destination + ", " + first);
      }
    } else {
      lines.add("  li " + INT_SCRATCH_0 + ", " + first);
      lines.add("  vadd.vx " + destination + ", " + destination + ", " + INT_SCRATCH_0);
    }
    return true;
  }

  private void emitIntegerVectorSplat(String destination, long value, List<String> lines) {
    if (value >= -16 && value <= 15) {
      lines.add("  vmv.v.i " + destination + ", " + value);
    } else {
      lines.add("  li " + INT_SCRATCH_0 + ", " + value);
      lines.add("  vmv.v.x " + destination + ", " + INT_SCRATCH_0);
    }
  }

  private void emitVectorExtract(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    String vector = sourceReg(instruction.getOperands().get(0), allocation);
    MachineOperand index = instruction.getOperands().get(1);
    boolean laneZero = index instanceof ImmOperand immediate && immediate.getValue() == 0;
    if (laneZero) {
      // The scalar move reads element zero directly; no slide temporary is needed.
    } else if (index instanceof ImmOperand immediate && immediate.getValue() >= 0
        && immediate.getValue() <= 31) {
      lines.add("  vslidedown.vi v0, " + vector + ", " + immediate.getValue());
    } else {
      String indexRegister =
          operandRegisterOrScratch(
              lines, index, INT_SCRATCH_0, MachineType.I32, allocation);
      lines.add("  vslidedown.vx v0, " + vector + ", " + indexRegister);
    }
    String destination = destReg(instruction, allocation);
    String extractionSource = laneZero ? vector : "v0";
    if (instruction.getType().isFloat()) {
      lines.add("  vfmv.f.s " + destination + ", " + extractionSource);
    } else {
      lines.add("  vmv.x.s " + destination + ", " + extractionSource);
    }
  }

  private void emitVectorInsert(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    String destination = destReg(instruction, allocation);
    String vector = sourceReg(instruction.getOperands().get(0), allocation);
    VectorShape shape = instruction.getDest().getVectorShape();
    MachineOperand element = instruction.getOperands().get(1);
    MachineOperand index = instruction.getOperands().get(2);
    if (index instanceof ImmOperand immediate && immediate.getValue() == 0) {
      emitVectorRegisterMove(lines, destination, vector, shape);
      String elementRegister =
          operandRegisterOrScratch(
              lines,
              element,
              shape.elementType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0,
              shape.elementType(),
              allocation);
      lines.add(
          "  " + (shape.elementType().isFloat() ? "vfmv.s.f " : "vmv.s.x ")
              + destination + ", " + elementRegister);
      return;
    }
    lines.add("  vid.v " + destination);
    if (index instanceof ImmOperand immediate
        && immediate.getValue() >= -16 && immediate.getValue() <= 15) {
      lines.add("  vmseq.vi v0, " + destination + ", " + immediate.getValue());
    } else {
      String indexRegister =
          operandRegisterOrScratch(lines, index, INT_SCRATCH_0, MachineType.I32, allocation);
      lines.add("  vmseq.vx v0, " + destination + ", " + indexRegister);
    }
    String elementRegister =
        operandRegisterOrScratch(
            lines,
            element,
            shape.elementType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_1,
            shape.elementType(),
            allocation);
    lines.add(
        "  " + (shape.elementType().isFloat() ? "vfmerge.vfm " : "vmerge.vxm ")
            + destination + ", " + vector + ", " + elementRegister + ", v0");
  }

  private void emitVectorShuffle(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    String destination = destReg(instruction, allocation);
    VectorShape shape = instruction.getDest().getVectorShape();
    String left = sourceReg(instruction.getOperands().get(0), allocation);
    String right = sourceReg(instruction.getOperands().get(1), allocation);
    VectorConstantOperand mask = (VectorConstantOperand) instruction.getOperands().get(2);
    boolean hasUndefinedLane =
        mask.getElements().stream()
            .mapToLong(element -> ((Constant.Int) element).value)
            .anyMatch(selected -> selected < 0 || selected >= shape.lanes() * 2L);
    if (hasUndefinedLane) lines.add("  vmv.v.i " + destination + ", 0");
    for (int lane = 0; lane < mask.getElements().size(); lane++) {
      long selected = ((Constant.Int) mask.getElements().get(lane)).value;
      if (selected < 0 || selected >= shape.lanes() * 2L) continue;
      String source = selected < shape.lanes() ? left : right;
      long sourceLane = selected % shape.lanes();
      if (sourceLane <= 31) {
        lines.add("  vslidedown.vi v0, " + source + ", " + sourceLane);
      } else {
        lines.add("  li " + INT_SCRATCH_0 + ", " + sourceLane);
        lines.add("  vslidedown.vx v0, " + source + ", " + INT_SCRATCH_0);
      }
      if (lane == 0) {
        lines.add(
            "  " + (shape.elementType().isFloat() ? "vfmv.f.s " : "vmv.x.s ")
                + (shape.elementType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0) + ", v0");
        lines.add(
            "  " + (shape.elementType().isFloat() ? "vfmv.s.f " : "vmv.s.x ")
                + destination + ", "
                + (shape.elementType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0));
      } else {
        if (lane <= 31) {
          lines.add("  vslideup.vi " + destination + ", v0, " + lane);
        } else {
          lines.add("  li " + INT_SCRATCH_0 + ", " + lane);
          lines.add("  vslideup.vx " + destination + ", v0, " + INT_SCRATCH_0);
        }
      }
    }
  }

  private void emitVectorRegisterMove(
      List<String> lines, String destination, String source, VectorShape shape) {
    if (destination.equals(source)) return;
    lines.add("  vmv" + shape.lmul() + "r.v " + destination + ", " + source);
  }

  private void emitVectorOperation(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    MachineOpcode opcode = instruction.getOpcode();
    String destination = destReg(instruction, allocation);
    if (opcode == MachineOpcode.VSELECT) {
      MachineOperand condition = instruction.getOperands().get(0);
      if (condition instanceof ImmOperand immediate) {
        emitVectorRegisterMove(
            lines,
            destination,
            sourceReg(
                instruction.getOperands().get(immediate.getValue() != 0 ? 1 : 2), allocation),
            instruction.getDest().getVectorShape());
        return;
      }
      if (!(condition instanceof VRegOperand conditionOperand)
          || !conditionOperand.getRegister().getType().isVector()) {
        int id = selectLabelCounter++;
        String done = ".L_vector_select_done_" + id;
        emitVectorRegisterMove(
            lines,
            destination,
            sourceReg(instruction.getOperands().get(2), allocation),
            instruction.getDest().getVectorShape());
        lines.add(
            "  beqz " + sourceReg(condition, allocation) + ", " + done);
        emitVectorRegisterMove(
            lines,
            destination,
            sourceReg(instruction.getOperands().get(1), allocation),
            instruction.getDest().getVectorShape());
        lines.add(done + ":");
        return;
      }
      String mask = sourceReg(instruction.getOperands().get(0), allocation);
      emitVectorRegisterMove(
          lines, "v0", mask, conditionOperand.getRegister().getVectorShape());
      lines.add(
          "  vmerge.vvm " + destination + ", "
              + sourceReg(instruction.getOperands().get(2), allocation) + ", "
              + sourceReg(instruction.getOperands().get(1), allocation) + ", v0");
      return;
    }
    if (opcode == MachineOpcode.VZEXT) {
      VectorShape sourceShape =
          ((VRegOperand) instruction.getOperands().get(0)).getRegister().getVectorShape();
      String source = sourceReg(instruction.getOperands().get(0), allocation);
      if (sourceShape.mask()) {
        emitVectorRegisterMove(lines, "v0", source, sourceShape);
        lines.add("  vmv.v.i " + destination + ", 0");
        lines.add("  vmerge.vim " + destination + ", " + destination + ", 1, v0");
      } else {
        int factor = instruction.getDest().getVectorShape().sew() / sourceShape.sew();
        lines.add("  vzext.vf" + factor + " " + destination + ", " + source);
      }
      return;
    }
    if (opcode == MachineOpcode.VSEXT) {
      VectorShape sourceShape =
          ((VRegOperand) instruction.getOperands().get(0)).getRegister().getVectorShape();
      VectorShape destinationShape = instruction.getDest().getVectorShape();
      int factor = destinationShape.sew() / sourceShape.sew();
      lines.add(
          "  vsext.vf" + factor + " " + destination + ", "
              + sourceReg(instruction.getOperands().get(0), allocation));
      return;
    }
    if (opcode == MachineOpcode.VSITOFP || opcode == MachineOpcode.VFPTOSI) {
      lines.add(
          "  " + (opcode == MachineOpcode.VSITOFP ? "vfcvt.f.x.v " : "vfcvt.rtz.x.f.v ")
              + destination + ", " + sourceReg(instruction.getOperands().get(0), allocation));
      return;
    }
    if (opcode == MachineOpcode.VFNEG) {
      String source = sourceReg(instruction.getOperands().get(0), allocation);
      lines.add("  vfsgnjn.vv " + destination + ", " + source + ", " + source);
      return;
    }
    if (opcode == MachineOpcode.VICMP || opcode == MachineOpcode.VFCMP) {
      emitVectorCompare(instruction, allocation, lines);
      return;
    }
    String mnemonic = switch (opcode) {
      case VADD -> "vadd.vv";
      case VSUB -> "vsub.vv";
      case VMUL -> "vmul.vv";
      case VDIV -> "vdiv.vv";
      case VREM -> "vrem.vv";
      case VSHL -> "vsll.vv";
      case VASHR -> "vsra.vv";
      case VAND -> "vand.vv";
      case VXOR -> "vxor.vv";
      case VFADD -> "vfadd.vv";
      case VFSUB -> "vfsub.vv";
      case VFMUL -> "vfmul.vv";
      case VFDIV -> "vfdiv.vv";
      default -> throw new IllegalStateException("unsupported vector operation: " + opcode);
    };
    lines.add(
        "  " + mnemonic + " " + destination + ", "
            + sourceReg(instruction.getOperands().get(0), allocation) + ", "
            + sourceReg(instruction.getOperands().get(1), allocation));
  }

  private void emitVectorCompare(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    boolean floating = instruction.getOpcode() == MachineOpcode.VFCMP;
    String left = sourceReg(instruction.getOperands().get(0), allocation);
    String right = sourceReg(instruction.getOperands().get(1), allocation);
    String predicate = instruction.getPredicate();
    if (floating && (predicate.equals("one") || predicate.equals("ueq"))) {
      String destination = destReg(instruction, allocation);
      lines.add("  vmfeq.vv " + destination + ", " + left + ", " + left);
      lines.add("  vmfeq.vv v0, " + right + ", " + right);
      lines.add("  vmand.mm " + destination + ", " + destination + ", v0");
      lines.add("  vmfne.vv v0, " + left + ", " + right);
      lines.add("  vmand.mm " + destination + ", " + destination + ", v0");
      if (predicate.equals("ueq")) {
        lines.add("  vmnot.m " + destination + ", " + destination);
      }
      return;
    }
    if (floating
        && (predicate.equals("ult")
            || predicate.equals("ule")
            || predicate.equals("ugt")
            || predicate.equals("uge"))) {
      String destination = destReg(instruction, allocation);
      String orderedInverse =
          switch (predicate) {
            case "ult" -> "vmfle.vv " + destination + ", " + right + ", " + left;
            case "ule" -> "vmflt.vv " + destination + ", " + right + ", " + left;
            case "ugt" -> "vmfle.vv " + destination + ", " + left + ", " + right;
            case "uge" -> "vmflt.vv " + destination + ", " + left + ", " + right;
            default -> throw new IllegalStateException();
          };
      lines.add("  " + orderedInverse);
      lines.add("  vmnot.m " + destination + ", " + destination);
      return;
    }
    boolean swap =
        predicate.equals("sgt")
            || predicate.equals("ugt")
            || predicate.equals("ogt")
            || predicate.equals("oge");
    String mnemonic =
        floating
            ? switch (predicate) {
              case "oeq" -> "vmfeq.vv";
              case "une" -> "vmfne.vv";
              case "olt", "ogt" -> "vmflt.vv";
              case "ole", "oge" -> "vmfle.vv";
              default -> throw new UnsupportedOperationException(
                  "unsupported vector FP predicate: " + predicate);
            }
            : switch (predicate) {
              case "eq" -> "vmseq.vv";
              case "ne" -> "vmsne.vv";
              case "slt", "sgt" -> "vmslt.vv";
              case "sle", "sge" -> "vmsle.vv";
              case "ult", "ugt" -> "vmsltu.vv";
              case "ule", "uge" -> "vmsleu.vv";
              default -> throw new UnsupportedOperationException(
                  "unsupported vector integer predicate: " + predicate);
            };
    if (!floating && (predicate.equals("sge") || predicate.equals("uge"))) {
      lines.add(
          "  " + (predicate.equals("sge") ? "vmslt.vv " : "vmsltu.vv ")
              + destReg(instruction, allocation) + ", " + left + ", " + right);
      lines.add("  vmnot.m " + destReg(instruction, allocation) + ", " + destReg(instruction, allocation));
      return;
    }
    lines.add(
        "  " + mnemonic + " " + destReg(instruction, allocation) + ", "
            + (swap ? right : left) + ", " + (swap ? left : right));
  }

  private void emitVCIX(
      MachineInstr instruction, AllocationResult allocation, List<String> lines) {
    VCIXInfo info = instruction.getVCIXInfo();
    if (info == null) throw new IllegalArgumentException("VCIX instruction has no encoding info");
    VCIXInfo.OperandForm form = info.form();
    int operandOffset = form.readsDestination() ? 1 : 0;
    int vd;
    if (info.writesVectorDestination()) {
      vd = allocatedPhysical(instruction.getDest(), allocation).getEncoding();
      if (form.readsDestination()) {
        String oldDestination = sourceReg(instruction.getOperands().get(0), allocation);
        emitVectorRegisterMove(
            lines,
            "v" + vd,
            oldDestination,
            instruction.getDest().getVectorShape());
      }
    } else if (form.readsDestination()) {
      vd = vectorEncoding(instruction.getOperands().get(0), allocation);
    } else {
      vd = info.rdCustom();
    }

    int rs2 =
        form.hasVectorSource2()
            ? vectorEncoding(instruction.getOperands().get(operandOffset), allocation)
            : info.rs2Custom();
    int argumentIndex = form.hasVectorSource2() ? operandOffset + 1 : 0;
    int rs1;
    if (form.hasImmediate()) {
      long immediate = ((ImmOperand) instruction.getOperands().get(argumentIndex)).getValue();
      if (immediate < -16 || immediate > 15) {
        throw new IllegalArgumentException("VCIX immediate is outside signed five-bit range");
      }
      rs1 = (int) immediate & 0x1f;
    } else if (form.hasVectorSource1()) {
      rs1 = vectorEncoding(instruction.getOperands().get(argumentIndex), allocation);
    } else {
      String argumentRegister =
          operandRegisterOrScratch(
              lines,
              instruction.getOperands().get(argumentIndex),
              form.hasFloatScalar() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0,
              form.hasFloatScalar() ? MachineType.F32 : MachineType.I32,
              allocation);
      rs1 = riscvRegisterEncoding(argumentRegister);
    }

    int encoded = VCIXEncoder.encode(info, vd, rs2, rs1);
    lines.add(
        String.format(
            "  .4byte 0x%08x # %s", Integer.toUnsignedLong(encoded), info.mnemonic()));
  }

  private int vectorEncoding(MachineOperand operand, AllocationResult allocation) {
    if (operand instanceof PhysicalRegOperand physical) {
      int encoding = physical.getRegister().getEncoding();
      if (encoding >= 0) return encoding;
      return parseVectorRegister(physical.getRegister().getName());
    }
    if (operand instanceof VRegOperand virtual) {
      return allocatedPhysical(virtual.getRegister(), allocation).getEncoding();
    }
    throw new IllegalArgumentException("VCIX vector operand must be a register");
  }

  private PhysicalRegister allocatedPhysical(
      VirtualRegister register, AllocationResult allocation) {
    ValueLocation location = allocation.locationOf(register);
    if (!(location instanceof RegisterLocation registerLocation)) {
      throw new IllegalStateException("VCIX operand was not allocated to a register");
    }
    return registerLocation.getRegister();
  }

  private static int parseVectorRegister(String name) {
    if (!name.startsWith("v")) throw new IllegalArgumentException("not a vector register: " + name);
    return Integer.parseInt(name.substring(1));
  }

  private static int riscvRegisterEncoding(String name) {
    String[] integerNames = {
      "zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2",
      "s0", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
      "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7",
      "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"
    };
    for (int index = 0; index < integerNames.length; index++) {
      if (integerNames[index].equals(name) || name.equals("fp") && index == 8) return index;
    }
    String[] floatNames = {
      "ft0", "ft1", "ft2", "ft3", "ft4", "ft5", "ft6", "ft7",
      "fs0", "fs1", "fa0", "fa1", "fa2", "fa3", "fa4", "fa5",
      "fa6", "fa7", "fs2", "fs3", "fs4", "fs5", "fs6", "fs7",
      "fs8", "fs9", "fs10", "fs11", "ft8", "ft9", "ft10", "ft11"
    };
    for (int index = 0; index < floatNames.length; index++) {
      if (floatNames[index].equals(name)) return index;
    }
    throw new IllegalArgumentException("unknown RISC-V register: " + name);
  }

  private void emitLoad(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    long offset =
        instr.getOperands().size() > 1 ? ((ImmOperand) instr.getOperands().get(1)).getValue() : 0;
    String address =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(0),
            INT_SCRATCH_0,
            MachineType.PTR,
            allocation);
    String dst = destReg(instr, allocation);
    lines.add("  " + loadMnemonic(instr.getType()) + " " + dst + ", " + offset + "(" + address + ")");
  }

  private void emitStore(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    long offset =
        instr.getOperands().size() > 2 ? ((ImmOperand) instr.getOperands().get(2)).getValue() : 0;
    MachineOperand storedValue = instr.getOperands().get(0);
    String value =
        !instr.getType().isFloat()
                && storedValue instanceof ImmOperand immediate
                && immediate.getValue() == 0
            ? "zero"
            : operandRegisterOrScratch(
                lines,
                storedValue,
                instr.getType().isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0,
                instr.getType(),
                allocation);
    String address =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(1),
            INT_SCRATCH_1,
            MachineType.PTR,
            allocation);
    lines.add(
        "  " + storeMnemonic(instr.getType()) + " " + value + ", " + offset + "(" + address + ")");
  }

  private void emitMemzero(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    int size = (int) ((ImmOperand) instr.getOperands().get(1)).getValue();
    String address =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(0),
            INT_SCRATCH_0,
            MachineType.PTR,
            allocation);
    if (target.shouldUseMemzeroHelper(size)) {
      emitRegisterMove(lines, "a0", address, MachineType.PTR);
      lines.add("  li a1, " + size);
      lines.add("  call __accela_memzero");
      return;
    }
    int offset = 0;
    for (; offset + 8 <= size; offset += 8)
      frameLowering.emitStoreToBase(
          lines, "zero", address, offset, ADDRESS_SCRATCH, MachineType.I64);
    if (offset < size)
      frameLowering.emitStoreToBase(
          lines, "zero", address, offset, ADDRESS_SCRATCH, MachineType.I32);
  }

  private void emitCall(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    RISCVTarget.CallArgCursor argCursor = target.newCallArgCursor();
    List<CallArgument> arguments = new ArrayList<>();
    for (int i = 0; i < instr.getOperands().size(); i++) {
      MachineOperand operand = instr.getOperands().get(i);
      rejectFixedArgumentRegisterSource(operand);
      MachineType argType = callOperandType(instr, i, operand);
      RISCVTarget.CallArgAssignment assignment = target.assignCallArg(argCursor, argType);
      arguments.add(new CallArgument(operand, argType, assignment));
    }

    // Stack arguments may need the reserved argument registers as materialization
    // scratch, so write them before committing any register arguments.
    for (CallArgument argument : arguments) {
      if (!argument.assignment.isInRegister()) {
        emitCallArgumentToStack(argument, allocation, lines);
      }
    }

    // Floating-point materialization can use an integer scratch register. Commit
    // fa0-fa7 before a0-a7 so those temporary integer writes cannot destroy a
    // finalized integer argument.
    for (CallArgument argument : arguments) {
      if (argument.assignment.isInRegister()
          && argument.assignment.getRegister().getType().isFloat()) {
        emitFloatCallArgumentToRegister(argument, allocation, lines);
      }
    }
    for (CallArgument argument : arguments) {
      if (argument.assignment.isInRegister()
          && !argument.assignment.getRegister().getType().isFloat()) {
        emitIntegerCallArgumentToRegister(argument, allocation, lines);
      }
    }

    lines.add("  call " + instr.getCallee());
    if (instr.getDest() != null) {
      emitRegisterMove(
          lines,
          destReg(instr, allocation),
          target.getReturnRegister(instr.getType()).getName(),
          instr.getType());
    }
  }

  private void rejectFixedArgumentRegisterSource(MachineOperand operand) {
    if (!(operand instanceof PhysicalRegOperand physical)) {
      return;
    }
    String name = physical.getRegister().getName();
    String index =
        name.startsWith("fa")
            ? name.substring(2)
            : name.startsWith("a") ? name.substring(1) : "";
    if (index.length() == 1 && index.charAt(0) >= '0' && index.charAt(0) <= '7') {
      throw new UnsupportedOperationException(
          "CALL source in fixed argument register must be copied to a virtual register: "
              + name);
    }
  }

  private void emitCallArgumentToStack(
      CallArgument argument, AllocationResult allocation, List<String> lines) {
    String source = callStackArgumentSource(argument, allocation, lines);
    frameLowering.emitStoreToBase(
        lines,
        source,
        "sp",
        argument.assignment.getStackOffset(),
        source.equals(ADDRESS_SCRATCH) ? "a4" : ADDRESS_SCRATCH,
        argument.type);
  }

  private String callStackArgumentSource(
      CallArgument argument, AllocationResult allocation, List<String> lines) {
    MachineOperand operand = argument.operand;
    if (operand instanceof VRegOperand) {
      return sourceReg(operand, allocation);
    }
    if (operand instanceof PhysicalRegOperand physical) {
      if (argument.type.isFloat() && !physical.getRegister().getType().isFloat()) {
        lines.add(
            "  fmv.w.x "
                + FLOAT_SCRATCH_0
                + ", "
                + physical.getRegister().getName());
        return FLOAT_SCRATCH_0;
      }
      return physical.getRegister().getName();
    }

    String scratch = argument.type.isFloat() ? FLOAT_SCRATCH_0 : INT_SCRATCH_0;
    emitMoveToRegister(lines, operand, scratch, argument.type, allocation);
    return scratch;
  }

  private void emitFloatCallArgumentToRegister(
      CallArgument argument, AllocationResult allocation, List<String> lines) {
    String destination = argument.assignment.getRegister().getName();
    if (argument.operand instanceof PhysicalRegOperand physical
        && !physical.getRegister().getType().isFloat()) {
      lines.add("  fmv.w.x " + destination + ", " + physical.getRegister().getName());
      return;
    }
    emitMoveToRegister(lines, argument.operand, destination, argument.type, allocation);
  }

  private void emitIntegerCallArgumentToRegister(
      CallArgument argument, AllocationResult allocation, List<String> lines) {
    String destination = argument.assignment.getRegister().getName();
    if (argument.type.isFloat()) {
      emitFloatCallArgumentToIntegerRegister(
          argument.operand, destination, allocation, lines);
      return;
    }
    if (argument.operand instanceof StackSlotOperand stackSlot) {
      frameLowering.emitLoadFromBase(
          lines,
          destination,
          "sp",
          stackSlot.getSlot().getOffset(),
          destination,
          argument.type);
      return;
    }
    emitMoveToRegister(lines, argument.operand, destination, argument.type, allocation);
  }

  private void emitFloatCallArgumentToIntegerRegister(
      MachineOperand operand,
      String destination,
      AllocationResult allocation,
      List<String> lines) {
    if (operand instanceof VRegOperand) {
      lines.add("  fmv.x.w " + destination + ", " + sourceReg(operand, allocation));
      return;
    }
    if (operand instanceof PhysicalRegOperand physical) {
      if (physical.getRegister().getType().isFloat()) {
        lines.add("  fmv.x.w " + destination + ", " + physical.getRegister().getName());
      } else {
        emitRegisterMove(
            lines, destination, physical.getRegister().getName(), MachineType.I32);
      }
      return;
    }
    if (operand instanceof FloatImmOperand immediate) {
      int bits = java.lang.Float.floatToRawIntBits(immediate.getValue());
      lines.add("  li " + destination + ", " + bits);
      return;
    }
    if (operand instanceof ImmOperand immediate) {
      lines.add("  li " + destination + ", " + immediate.getValue());
      return;
    }
    if (operand instanceof StackSlotOperand stackSlot) {
      frameLowering.emitLoadFromBase(
          lines,
          destination,
          "sp",
          stackSlot.getSlot().getOffset(),
          destination,
          MachineType.I32);
      return;
    }
    if (operand instanceof SymbolOperand symbol) {
      lines.add("  la " + destination + ", " + symbol.getSymbol());
      return;
    }
    throw new UnsupportedOperationException(
        "unsupported floating-point call argument: " + operand.getKind());
  }

  private void emitFloatBinary(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    lines.add(
        "  "
            + floatBinaryMnemonic(instr.getOpcode())
            + " "
            + destReg(instr, allocation)
            + ", "
            + operandRegisterOrScratch(
                lines,
                instr.getOperands().get(0),
                FLOAT_SCRATCH_0,
                MachineType.F32,
                allocation)
            + ", "
            + operandRegisterOrScratch(
                lines,
                instr.getOperands().get(1),
                FLOAT_SCRATCH_1,
                MachineType.F32,
                allocation));
  }

  private void emitFloatCompare(MachineInstr instr, AllocationResult allocation, List<String> lines) {
    String dst = destReg(instr, allocation);
    String lhs =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(0),
            FLOAT_SCRATCH_0,
            MachineType.F32,
            allocation);
    String rhs =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(1),
            FLOAT_SCRATCH_1,
            MachineType.F32,
            allocation);
    switch (instr.getPredicate()) {
      case "oeq":
        lines.add("  feq.s " + dst + ", " + lhs + ", " + rhs);
        break;
      case "one":
      case "une":
        lines.add("  feq.s " + dst + ", " + lhs + ", " + rhs);
        lines.add("  seqz " + dst + ", " + dst);
        break;
      case "olt":
        lines.add("  flt.s " + dst + ", " + lhs + ", " + rhs);
        break;
      case "ogt":
        lines.add("  flt.s " + dst + ", " + rhs + ", " + lhs);
        break;
      case "ole":
        lines.add("  fle.s " + dst + ", " + lhs + ", " + rhs);
        break;
      case "oge":
        lines.add("  fle.s " + dst + ", " + rhs + ", " + lhs);
        break;
      default:
        throw new UnsupportedOperationException("Unsupported float compare predicate: " + instr.getPredicate());
    }
  }

  private void emitMoveToRegister(
      List<String> lines,
      MachineOperand operand,
      String dstReg,
      MachineType type,
      AllocationResult allocation) {
    if (operand instanceof ImmOperand) {
      if (type.isFloat()) {
        lines.add("  li " + INT_SCRATCH_1 + ", " + ((ImmOperand) operand).getValue());
        lines.add("  fmv.w.x " + dstReg + ", " + INT_SCRATCH_1);
      } else {
        lines.add("  li " + dstReg + ", " + ((ImmOperand) operand).getValue());
      }
      return;
    }
    if (operand instanceof FloatImmOperand) {
      int bits = java.lang.Float.floatToRawIntBits(((FloatImmOperand) operand).getValue());
      lines.add("  li " + INT_SCRATCH_1 + ", " + bits);
      lines.add("  fmv.w.x " + dstReg + ", " + INT_SCRATCH_1);
      return;
    }
    if (operand instanceof SymbolOperand) {
      lines.add("  la " + dstReg + ", " + ((SymbolOperand) operand).getSymbol());
      return;
    }
    if (operand instanceof StackSlotOperand) {
      StackSlot slot = ((StackSlotOperand) operand).getSlot();
      frameLowering.emitLoadFromBase(
          lines, dstReg, "sp", slot.getOffset(), ADDRESS_SCRATCH, type);
      return;
    }

    emitRegisterMove(lines, dstReg, sourceReg(operand, allocation), type);
  }

  private void emitRegisterMove(List<String> lines, String dstReg, String srcReg, MachineType type) {
    if (dstReg.equals(srcReg)) {
      return;
    }
    if (type.isFloat()) {
      lines.add("  fsgnj.s " + dstReg + ", " + srcReg + ", " + srcReg);
    } else {
      lines.add("  mv " + dstReg + ", " + srcReg);
    }
  }

  private String destReg(MachineInstr instr, AllocationResult allocation) {
    if (instr.getDest() == null) {
      throw new IllegalArgumentException("instruction has no destination: " + instr.getOpcode());
    }
    return registerLocation(instr.getDest(), allocation);
  }

  private String sourceReg(MachineOperand operand, AllocationResult allocation) {
    if (operand instanceof PhysicalRegOperand) {
      return ((PhysicalRegOperand) operand).getRegister().getName();
    }
    if (operand instanceof VRegOperand) {
      return registerLocation(((VRegOperand) operand).getRegister(), allocation);
    }
    throw new UnsupportedOperationException("expected register operand, got " + operand.getKind());
  }

  private String operandRegisterOrScratch(
      List<String> lines,
      MachineOperand operand,
      String scratch,
      MachineType type,
      AllocationResult allocation) {
    if (operand instanceof VRegOperand || operand instanceof PhysicalRegOperand) {
      return sourceReg(operand, allocation);
    }
    emitMoveToRegister(lines, operand, scratch, type, allocation);
    return scratch;
  }

  private String registerLocation(VirtualRegister register, AllocationResult allocation) {
    ValueLocation location = allocation.locationOf(register);
    if (location == null) {
      throw new IllegalStateException("Missing allocation for " + register);
    }
    if (!location.isRegister()) {
      throw new IllegalStateException("register allocation left stack location for " + register);
    }
    return ((RegisterLocation) location).getRegister().getName();
  }

  private String integerBinaryMnemonic(MachineOpcode opcode) {
    switch (opcode) {
      case ADD:
        return "add";
      case SUB:
        return "sub";
      case MUL:
        return "mul";
      case DIV:
        return "div";
      case REM:
        return "rem";
      case XOR:
        return "xor";
      default:
        throw new IllegalStateException();
    }
  }

  private String floatBinaryMnemonic(MachineOpcode opcode) {
    switch (opcode) {
      case FADD:
        return "fadd.s";
      case FSUB:
        return "fsub.s";
      case FMUL:
        return "fmul.s";
      case FDIV:
        return "fdiv.s";
      default:
        throw new IllegalStateException();
    }
  }

  private String loadMnemonic(MachineType type) {
    if (type.isFloat()) {
      return "flw";
    }
    return frameLowering.loadMnemonic(type);
  }

  private String storeMnemonic(MachineType type) {
    if (type.isFloat()) {
      return "fsw";
    }
    return frameLowering.storeMnemonic(type);
  }

  private boolean fitsSigned12(ImmOperand operand) {
    long value = operand.getValue();
    return value >= -2048 && value <= 2047;
  }

  private MachineType inferOperandType(MachineOperand operand) {
    if (operand instanceof VRegOperand) return ((VRegOperand) operand).getRegister().getType();
    if (operand instanceof FloatImmOperand) return MachineType.F32;
    if (operand instanceof SymbolOperand) return MachineType.PTR;
    if (operand instanceof StackSlotOperand) return ((StackSlotOperand) operand).getSlot().getType();
    if (operand instanceof PhysicalRegOperand) return ((PhysicalRegOperand) operand).getRegister().getType();
    if (operand instanceof ImmOperand) return MachineType.I32;
    return MachineType.I32;
  }

  private MachineType callOperandType(
      MachineInstr call, int operandIndex, MachineOperand operand) {
    MachineType recorded = call.getOperandType(operandIndex);
    return recorded != null ? recorded : inferOperandType(operand);
  }

  private static final class CallArgument {
    private final MachineOperand operand;
    private final MachineType type;
    private final RISCVTarget.CallArgAssignment assignment;

    private CallArgument(
        MachineOperand operand,
        MachineType type,
        RISCVTarget.CallArgAssignment assignment) {
      this.operand = operand;
      this.type = type;
      this.assignment = assignment;
    }
  }

  private String labelFor(MachineFunction function, MachineBasicBlock block) {
    return ".L_" + function.getName() + "_" + block.getLabel().replace('.', '_');
  }

  private void ensureIntType(MachineType type) {
    if (type.isFloat()) {
      throw new UnsupportedOperationException("integer opcode cannot produce float value");
    }
  }
}
