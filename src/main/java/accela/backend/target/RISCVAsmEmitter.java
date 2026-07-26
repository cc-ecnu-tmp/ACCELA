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
      new RISCVStrengthReduction(ADDRESS_SCRATCH, INT_SCRATCH_1);

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
    if ((opcode == MachineOpcode.ADD || opcode == MachineOpcode.MUL || opcode == MachineOpcode.XOR)
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
    lines.add("  mul " + ADDRESS_SCRATCH + ", " + lhs + ", " + rhs);
    lines.add("  srai " + dst + ", " + ADDRESS_SCRATCH + ", " + Integer.SIZE);
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
    String value =
        operandRegisterOrScratch(
            lines,
            instr.getOperands().get(0),
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
