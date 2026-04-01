package accela.backend;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.Map;

final class IRToMachineLowering {
  private final RISCVTarget target;

  IRToMachineLowering(RISCVTarget target) {
    this.target = target;
  }

  MachineModule lower(accela.ir.Module module) {
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
      case ADD:
        emitBinary(
            block,
            MachineOpcode.ADD,
            valueToVReg.get(inst),
            MachineType.I32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case SUB:
        emitBinary(
            block,
            MachineOpcode.SUB,
            valueToVReg.get(inst),
            MachineType.I32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case MUL:
        emitBinary(
            block,
            MachineOpcode.MUL,
            valueToVReg.get(inst),
            MachineType.I32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case SDIV:
        emitBinary(
            block,
            MachineOpcode.DIV,
            valueToVReg.get(inst),
            MachineType.I32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case SREM:
        emitBinary(
            block,
            MachineOpcode.REM,
            valueToVReg.get(inst),
            MachineType.I32,
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case XOR:
        emitBinary(
            block,
            MachineOpcode.XOR,
            valueToVReg.get(inst),
            MachineType.fromIr(inst.getType()),
            lowerValue(inst.getOperand(0), valueToVReg, blocks),
            lowerValue(inst.getOperand(1), valueToVReg, blocks));
        return;
      case ICMP:
        lowerICmp(inst, block, valueToVReg, blocks);
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
        MachineInstr condBr = new MachineInstr(MachineOpcode.CONDBR, null);
        condBr.addOperand(lowerValue(inst.getOperand(0), valueToVReg, blocks));
        condBr.addOperand(new BlockOperand(blocks.get((BasicBlock) inst.getOperand(1))));
        condBr.addOperand(new BlockOperand(blocks.get((BasicBlock) inst.getOperand(2))));
        block.addInstruction(condBr);
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
      MachineInstr memzero = new MachineInstr(MachineOpcode.MEMZERO, null);
      memzero.addOperand(lowerValue(inst.getOperand(1), valueToVReg, blocks));
      memzero.addOperand(new ImmOperand(target.sizeOfIrType(value.getType())));
      memzero.setType(MachineType.PTR);
      block.addInstruction(memzero);
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
      call.addOperand(operand);
      target.assignCallArg(argCursor, inferCallOperandType(operand));
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
      MachineOperand contribution = index;
      if (stride != 1) {
        VirtualRegister scaled = function.createVirtualRegister(MachineType.PTR, "gep.mul");
        emitBinary(block, MachineOpcode.MUL, scaled, MachineType.PTR, index, new ImmOperand(stride));
        contribution = new VRegOperand(scaled);
      }
      if (accumulated == null) {
        accumulated = contribution;
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

  private MachineType inferCallOperandType(MachineOperand operand) {
    if (operand instanceof VRegOperand) return ((VRegOperand) operand).getRegister().getType();
    if (operand instanceof FloatImmOperand) return MachineType.F32;
    if (operand instanceof SymbolOperand) return MachineType.PTR;
    if (operand instanceof StackSlotOperand) return ((StackSlotOperand) operand).getSlot().getType();
    if (operand instanceof PhysicalRegOperand) return ((PhysicalRegOperand) operand).getRegister().getType();
    if (operand instanceof ImmOperand) return MachineType.I32;
    return MachineType.I32;
  }
}
