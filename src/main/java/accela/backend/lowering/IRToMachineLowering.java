package accela.backend.lowering;

import accela.backend.frame.StackSlot;
import accela.backend.machine.BlockOperand;
import accela.backend.machine.FloatImmOperand;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegOperand;
import accela.backend.machine.StackSlotOperand;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.Map;

public final class IRToMachineLowering {
  private final RISCVTarget target;

  public IRToMachineLowering(RISCVTarget target) {
    this.target = target;
  }

  public MachineModule lower(accela.ir.Module module) {
    MachineModule machineModule = new MachineModule(module);
    for (Function function : module.getFunctions()) {
      machineModule.addFunction(function, lowerFunction(function));
    }
    return machineModule;
  }

  private MachineFunction lowerFunction(Function function) {
    MachineFunction machineFunction =
        new MachineFunction(function.getName(), MachineType.fromIr(function.getReturnType()));

    Map<Value, VirtualRegister> valueToVReg = new IdentityHashMap<>();
    for (Function.Argument argument : function.getArguments()) {
      VirtualRegister vreg =
          machineFunction.createVirtualRegister(MachineType.fromIr(argument.getType()), argument.getName());
      machineFunction.addArgument(vreg, MachineType.fromIr(argument.getType()));
      valueToVReg.put(argument, vreg);
    }

    for (BasicBlock block : function.getBlocks()) {
      for (Instruction inst : block.getInstructions()) {
        if (inst.hasResult()) {
          valueToVReg.put(
              inst,
              machineFunction.createVirtualRegister(
                  MachineType.fromIr(inst.getType()),
                  inst.getName() != null ? inst.getName() : inst.getOpcode().name().toLowerCase()));
        }
      }
    }

    Map<BasicBlock, MachineBasicBlock> blocks = new IdentityHashMap<>();
    for (BasicBlock block : function.getBlocks()) {
      MachineBasicBlock machineBlock = machineFunction.addBlock(block.getLabel());
      machineBlock.setSourceBlock(block);
      machineBlock.setSourceFunction(function);
      blocks.put(block, machineBlock);
    }

    MachineBasicBlock entry = machineFunction.getEntryBlock();
    RISCVTarget.CallArgCursor argCursor = target.newCallArgCursor();
    for (int i = 0; i < function.getArguments().size(); i++) {
      VirtualRegister argVReg = valueToVReg.get(function.getArguments().get(i));
      MachineInstr argIn = new MachineInstr(MachineOpcode.ARG_IN, argVReg);
      RISCVTarget.CallArgAssignment assignment = target.assignCallArg(argCursor, argVReg.getType());
      if (assignment.isInRegister()) {
        argIn.addOperand(new PhysicalRegOperand(assignment.getRegister()));
      } else {
        argIn.addOperand(new ImmOperand(assignment.getStackOffset()));
      }
      argIn.setType(argVReg.getType());
      entry.addInstruction(argIn);
    }

    for (BasicBlock block : function.getBlocks()) {
      MachineBasicBlock machineBlock = blocks.get(block);
      for (Instruction inst : block.getInstructions()) {
        lowerInstruction(inst, machineBlock, machineFunction, valueToVReg, blocks);
      }
    }

    return machineFunction;
  }

  private void lowerInstruction(
      Instruction inst,
      MachineBasicBlock block,
      MachineFunction function,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    switch (inst.getOpcode()) {
      case ALLOCA:
        lowerAlloca(inst, block, function, valueToVReg);
        return;
      case LOAD:
        emitSimple(
            block,
            MachineOpcode.LOAD,
            valueToVReg.get(inst),
            MachineType.fromIr(inst.getType()),
            lowerValue(inst.getOperand(0), valueToVReg, blocks));
        return;
      case STORE:
        lowerStore(inst, block, function, valueToVReg, blocks);
        return;
      case GEP:
        lowerGep(inst, block, function, valueToVReg, blocks);
        return;
      case SMULH:
        if (!isFusedSMulH(inst)) {
          lowerIntegerBinary(inst, block, valueToVReg, blocks);
        }
        return;
      case ASHR:
        Instruction multiplyHigh = fusedSMulH(inst);
        if (multiplyHigh == null) {
          lowerIntegerBinary(inst, block, valueToVReg, blocks);
        } else {
          lowerSMulHShift(inst, multiplyHigh, block, valueToVReg, blocks);
        }
        return;
      case ADD, SUB, MUL, SDIV, SREM, SHL, AND, XOR:
        lowerIntegerBinary(inst, block, valueToVReg, blocks);
        return;
      case ICMP:
        if (!canFuseCompareBranch(inst)) {
          lowerICmp(inst, block, valueToVReg, blocks);
        }
        return;
      case FCMP:
        lowerFCmp(inst, block, valueToVReg, blocks);
        return;
      case ZEXT:
        emitSimple(
            block,
            MachineOpcode.ZEXT,
            valueToVReg.get(inst),
            MachineType.fromIr(inst.getType()),
            lowerValue(inst.getOperand(0), valueToVReg, blocks));
        return;
      case SEXT:
        emitSimple(
            block,
            MachineOpcode.SEXT,
            valueToVReg.get(inst),
            MachineType.fromIr(inst.getType()),
            lowerValue(inst.getOperand(0), valueToVReg, blocks));
        return;
      case SITOFP:
        emitSimple(
            block,
            MachineOpcode.SITOFP,
            valueToVReg.get(inst),
            MachineType.F32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks));
        return;
      case FPTOSI:
        emitSimple(
            block,
            MachineOpcode.FPTOSI,
            valueToVReg.get(inst),
            MachineType.I32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks));
        return;
      case BR:
        MachineInstr br = new MachineInstr(MachineOpcode.BR, null);
        br.addOperand(new BlockOperand(blocks.get((BasicBlock) inst.getOperand(0))));
        block.addInstruction(br);
        return;
      case CONDBR:
        lowerCondBr(inst, block, valueToVReg, blocks);
        return;
      case RET:
        MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
        if (inst.getNumOperands() > 0) {
          ret.addOperand(lowerValue(inst.getOperand(0), valueToVReg, blocks));
          ret.setType(MachineType.fromIr(inst.getOperand(0).getType()));
        } else {
          ret.setType(MachineType.VOID);
        }
        block.addInstruction(ret);
        return;
      case CALL:
        lowerCall(inst, block, function, valueToVReg, blocks);
        return;
      case PHI:
        lowerPhi(inst, block, valueToVReg, blocks);
        return;
      case FADD:
        emitBinary(
            block,
            MachineOpcode.FADD,
            valueToVReg.get(inst),
            MachineType.F32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case FSUB:
        emitBinary(
            block,
            MachineOpcode.FSUB,
            valueToVReg.get(inst),
            MachineType.F32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case FMUL:
        emitBinary(
            block,
            MachineOpcode.FMUL,
            valueToVReg.get(inst),
            MachineType.F32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case FDIV:
        emitBinary(
            block,
            MachineOpcode.FDIV,
            valueToVReg.get(inst),
            MachineType.F32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case FNEG:
        emitSimple(
            block,
            MachineOpcode.FNEG,
            valueToVReg.get(inst),
            MachineType.F32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks));
        return;
      default:
        throw new UnsupportedOperationException("Unsupported IR opcode: " + inst.getOpcode());
    }
  }

  private void lowerAlloca(
      Instruction inst,
      MachineBasicBlock block,
      MachineFunction function,
      Map<Value, VirtualRegister> valueToVReg) {
    Type allocType = inst.getAllocatedType();
    StackSlot slot =
        function
            .getFrameInfo()
            .createLocalSlot(
                MachineType.fromIr(allocType),
                target.sizeOfIrType(allocType),
                target.alignOfIrType(allocType));
    MachineInstr stackAddr = new MachineInstr(MachineOpcode.STACK_ADDR, valueToVReg.get(inst));
    stackAddr.addOperand(new StackSlotOperand(slot));
    stackAddr.setType(MachineType.PTR);
    block.addInstruction(stackAddr);
  }

  private void lowerStore(
      Instruction inst,
      MachineBasicBlock block,
      MachineFunction function,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    Value value = inst.getOperand(0);
    if (value instanceof Constant.Zero && value.getType().isArray()) {
      int size = target.sizeOfIrType(value.getType());
      MachineInstr memzero = new MachineInstr(MachineOpcode.MEMZERO, null);
      memzero.addOperand(lowerValue(inst.getOperand(1), valueToVReg, blocks));
      memzero.addOperand(new ImmOperand(size));
      memzero.setType(MachineType.PTR);
      block.addInstruction(memzero);
      if (target.shouldUseMemzeroHelper(size)) function.getFrameInfo().markHasCall();
      return;
    }

    MachineInstr store = new MachineInstr(MachineOpcode.STORE, null);
    store.addOperand(lowerValue(value, valueToVReg, blocks));
    store.addOperand(lowerValue(inst.getOperand(1), valueToVReg, blocks));
    store.setType(MachineType.fromIr(value.getType()));
    block.addInstruction(store);
  }

  private void lowerICmp(
      Instruction inst,
      MachineBasicBlock block,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    MachineInstr cmp = new MachineInstr(MachineOpcode.ICMP, valueToVReg.get(inst));
    cmp.addOperand(lowerValue(inst.getOperand(0), valueToVReg, blocks));
    cmp.addOperand(lowerValue(inst.getOperand(1), valueToVReg, blocks));
    cmp.setType(MachineType.I1);
    cmp.setPredicate(inst.getPredicate());
    block.addInstruction(cmp);
  }

  private void lowerCondBr(
      Instruction inst,
      MachineBasicBlock block,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    MachineInstr branch = new MachineInstr(MachineOpcode.CONDBR, null);
    Value condition = inst.getOperand(0);
    if (condition instanceof Instruction compare && canFuseCompareBranch(compare)) {
      branch.setPredicate(compare.getPredicate());
      branch.addOperand(lowerValue(compare.getOperand(0), valueToVReg, blocks));
      branch.addOperand(lowerValue(compare.getOperand(1), valueToVReg, blocks));
    } else {
      branch.addOperand(lowerValue(condition, valueToVReg, blocks));
    }
    branch.addOperand(new BlockOperand(blocks.get((BasicBlock) inst.getOperand(1))));
    branch.addOperand(new BlockOperand(blocks.get((BasicBlock) inst.getOperand(2))));
    block.addInstruction(branch);
  }

  private static boolean canFuseCompareBranch(Instruction compare) {
    if (compare.getOpcode() != Instruction.Opcode.ICMP || compare.getNumUses() != 1) return false;
    Instruction branch = compare.getUses().getFirst().getUser();
    var instructions = compare.getParent().getInstructions();
    return branch.getOpcode() == Instruction.Opcode.CONDBR
        && branch.getParent() == compare.getParent()
        && branch.getOperand(0) == compare
        && instructions.getLast() == branch;
  }

  private void lowerFCmp(
      Instruction inst,
      MachineBasicBlock block,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    MachineInstr cmp = new MachineInstr(MachineOpcode.FCMP, valueToVReg.get(inst));
    cmp.addOperand(lowerValue(inst.getOperand(0), valueToVReg, blocks));
    cmp.addOperand(lowerValue(inst.getOperand(1), valueToVReg, blocks));
    cmp.setType(MachineType.I1);
    cmp.setPredicate(inst.getPredicate());
    block.addInstruction(cmp);
  }

  private void lowerCall(
      Instruction inst,
      MachineBasicBlock block,
      MachineFunction function,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    VirtualRegister dest = inst.hasResult() ? valueToVReg.get(inst) : null;
    MachineInstr call = new MachineInstr(MachineOpcode.CALL, dest);
    call.setCallee(inst.getCallee().getName());
    call.setType(MachineType.fromIr(inst.getType()));
    RISCVTarget.CallArgCursor argCursor = target.newCallArgCursor();
    for (int i = 0; i < inst.getNumOperands(); i++) {
      MachineOperand operand = lowerValue(inst.getOperand(i), valueToVReg, blocks);
      MachineType argumentType = MachineType.fromIr(inst.getOperand(i).getType());
      call.addOperand(operand, argumentType);
      target.assignCallArg(argCursor, argumentType);
    }
    block.addInstruction(call);
    function.getFrameInfo().markHasCall();
    function.getFrameInfo().reserveOutgoingArgBytes(argCursor.getStackBytes());
  }

  private void lowerPhi(
      Instruction inst,
      MachineBasicBlock block,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    MachineInstr phi = new MachineInstr(MachineOpcode.PHI, valueToVReg.get(inst));
    phi.setType(MachineType.fromIr(inst.getType()));
    for (int i = 0; i < inst.getNumOperands(); i += 2) {
      phi.addOperand(lowerValue(inst.getOperand(i), valueToVReg, blocks));
      phi.addOperand(new BlockOperand(blocks.get((BasicBlock) inst.getOperand(i + 1))));
    }
    block.addInstruction(phi);
  }

  private void lowerGep(
      Instruction inst,
      MachineBasicBlock block,
      MachineFunction function,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    VirtualRegister dest = valueToVReg.get(inst);
    MachineOperand base = lowerValue(inst.getOperand(0), valueToVReg, blocks);
    Type currentType = inst.getGepSourceType();
    MachineOperand accumulated = null;

    for (int i = 1; i < inst.getNumOperands(); i++) {
      MachineOperand index = lowerValue(inst.getOperand(i), valueToVReg, blocks);
      int stride = target.sizeOfIrType(currentType);
      MachineOperand contribution;
      if (index instanceof ImmOperand immediate) {
        contribution = new ImmOperand(immediate.getValue() * stride);
      } else if (stride != 1) {
        VirtualRegister scaled = function.createVirtualRegister(MachineType.PTR, "gep.mul");
        emitBinary(block, MachineOpcode.MUL, scaled, MachineType.PTR, index, new ImmOperand(stride));
        contribution = new VRegOperand(scaled);
      } else {
        contribution = index;
      }
      if (contribution instanceof ImmOperand immediate && immediate.getValue() == 0) {
        // Keep descending the aggregate type, do nothing
      } else if (accumulated == null) {
        accumulated = contribution;
      } else if (accumulated instanceof ImmOperand left
          && contribution instanceof ImmOperand right) {
        accumulated = new ImmOperand(left.getValue() + right.getValue());
      } else {
        VirtualRegister next = function.createVirtualRegister(MachineType.PTR, "gep.add");
        emitBinary(block, MachineOpcode.ADD, next, MachineType.PTR, accumulated, contribution);
        accumulated = new VRegOperand(next);
      }
      if (currentType != null && currentType.isArray()) {
        currentType = currentType.innerType;
      }
    }

    if (accumulated == null) {
      emitSimple(block, MachineOpcode.MOVE, dest, MachineType.PTR, base);
      return;
    }

    emitBinary(block, MachineOpcode.ADD, dest, MachineType.PTR, base, accumulated);
  }

  private void lowerIntegerBinary(
      Instruction instruction,
      MachineBasicBlock block,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    MachineOpcode opcode = switch (instruction.getOpcode()) {
      case ADD -> MachineOpcode.ADD;
      case SUB -> MachineOpcode.SUB;
      case MUL -> MachineOpcode.MUL;
      case SMULH -> MachineOpcode.SMULH;
      case SDIV -> MachineOpcode.DIV;
      case SREM -> MachineOpcode.REM;
      case SHL -> MachineOpcode.SHL;
      case ASHR -> MachineOpcode.ASHR;
      case AND -> MachineOpcode.AND;
      case XOR -> MachineOpcode.XOR;
      default -> throw new IllegalStateException(
          "Not an integer binary instruction: " + instruction.getOpcode());
    };
    emitBinary(
        block,
        opcode,
        valueToVReg.get(instruction),
        MachineType.fromIr(instruction.getType()),
        lowerValue(instruction.getOperand(0), valueToVReg, blocks),
        lowerValue(instruction.getOperand(1), valueToVReg, blocks));
  }

  private void lowerSMulHShift(
      Instruction shift,
      Instruction multiplyHigh,
      MachineBasicBlock block,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    MachineInstr combined = new MachineInstr(MachineOpcode.SMULH, valueToVReg.get(shift));
    combined.setType(MachineType.I32);
    combined.addOperand(lowerValue(multiplyHigh.getOperand(0), valueToVReg, blocks));
    combined.addOperand(lowerValue(multiplyHigh.getOperand(1), valueToVReg, blocks));
    combined.addOperand(lowerValue(shift.getOperand(1), valueToVReg, blocks));
    block.addInstruction(combined);
  }

  private static boolean isFusedSMulH(Instruction multiplyHigh) {
    return multiplyHigh.getNumUses() == 1
        && fusedSMulH(multiplyHigh.getUses().getFirst().getUser()) == multiplyHigh;
  }

  private static Instruction fusedSMulH(Instruction shift) {
    if (shift.getOpcode() != Instruction.Opcode.ASHR
        || !(shift.getOperand(0) instanceof Instruction multiplyHigh)
        || multiplyHigh.getOpcode() != Instruction.Opcode.SMULH
        || multiplyHigh.getParent() != shift.getParent()
        || multiplyHigh.getNumUses() != 1
        || !(shift.getOperand(1) instanceof Constant.Int amount)
        || amount.value < 0
        || amount.value >= Integer.SIZE) return null;
    return multiplyHigh;
  }

  private void emitSimple(
      MachineBasicBlock block,
      MachineOpcode opcode,
      VirtualRegister dest,
      MachineType type,
      MachineOperand operand) {
    MachineInstr instr = new MachineInstr(opcode, dest);
    instr.addOperand(operand);
    instr.setType(type);
    block.addInstruction(instr);
  }

  private void emitBinary(
      MachineBasicBlock block,
      MachineOpcode opcode,
      VirtualRegister dest,
      MachineType type,
      MachineOperand lhs,
      MachineOperand rhs) {
    MachineInstr instr = new MachineInstr(opcode, dest);
    instr.addOperand(lhs);
    instr.addOperand(rhs);
    instr.setType(type);
    block.addInstruction(instr);
  }

  private MachineOperand lowerValue(
      Value value,
      Map<Value, VirtualRegister> valueToVReg,
      Map<BasicBlock, MachineBasicBlock> blocks) {
    if (value instanceof Constant.Int) {
      return new ImmOperand(target.lowerIntConstant((Constant.Int) value));
    }
    if (value instanceof Constant.Float) {
      return new FloatImmOperand(((Constant.Float) value).value);
    }
    if (value instanceof Constant.Zero && !value.getType().isArray()) {
      if (value.getType().isFloat()) return new FloatImmOperand(0.0f);
      return new ImmOperand(0);
    }
    if (value instanceof GlobalVariable) {
      return new SymbolOperand(((GlobalVariable) value).getName());
    }
    if (value instanceof BasicBlock) {
      return new BlockOperand(blocks.get((BasicBlock) value));
    }
    VirtualRegister register = valueToVReg.get(value);
    if (register != null) {
      return new VRegOperand(register);
    }
    throw new UnsupportedOperationException("Unsupported value in backend lowering: " + value.getClass().getName());
  }

}
