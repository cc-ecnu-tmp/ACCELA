package accela.backend;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Type;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

enum MachineType {
  I1(4),
  I32(4),
  I64(8),
  PTR(8),
  F32(4),
  VOID(0);

  private final int size;

  MachineType(int size) {
    this.size = size;
  }

  int getSize() {
    return size;
  }

  boolean isFloat() {
    return this == F32;
  }

  boolean isIntegerLike() {
    return this == I1 || this == I32 || this == I64 || this == PTR;
  }

  static MachineType fromIr(Type type) {
    if (type == null) return VOID;
    switch (type.dataType) {
      case I1:
        return I1;
      case INT:
        return I32;
      case I64:
        return I64;
      case FLOAT:
        return F32;
      case POINTER:
        return PTR;
      case VOID:
        return VOID;
      case ARRAY:
        return PTR;
      default:
        return I32;
    }
  }
}

final class VirtualRegister {
  private final int id;
  private final MachineType type;
  private final String hint;

  VirtualRegister(int id, MachineType type, String hint) {
    this.id = id;
    this.type = type;
    this.hint = hint;
  }

  int getId() {
    return id;
  }

  MachineType getType() {
    return type;
  }

  String getHint() {
    return hint;
  }

  @Override
  public boolean equals(Object other) {
    if (!(other instanceof VirtualRegister)) return false;
    return id == ((VirtualRegister) other).id;
  }

  @Override
  public int hashCode() {
    return id;
  }

  @Override
  public String toString() {
    return "%v" + id;
  }
}

final class PhysicalRegister {
  private final String name;
  private final MachineType type;

  PhysicalRegister(String name, MachineType type) {
    this.name = name;
    this.type = type;
  }

  String getName() {
    return name;
  }

  MachineType getType() {
    return type;
  }
}

enum MachineOpcode {
  ARG_IN,
  CONST_INT,
  STACK_ADDR,
  MOVE,
  ADD,
  SUB,
  MUL,
  DIV,
  REM,
  XOR,
  ICMP,
  FCMP,
  ZEXT,
  SEXT,
  SITOFP,
  FPTOSI,
  LOAD,
  STORE,
  MEMZERO,
  FADD,
  FSUB,
  FMUL,
  FDIV,
  FNEG,
  BR,
  CONDBR,
  CALL,
  RET,
  PHI
}

final class MachineInstr {
  private final MachineOpcode opcode;
  private final VirtualRegister dest;
  private final List<MachineOperand> operands = new ArrayList<>();
  private MachineType type = MachineType.VOID;
  private String predicate;
  private String callee;

  MachineInstr(MachineOpcode opcode, VirtualRegister dest) {
    this.opcode = opcode;
    this.dest = dest;
  }

  MachineOpcode getOpcode() {
    return opcode;
  }

  VirtualRegister getDest() {
    return dest;
  }

  List<MachineOperand> getOperands() {
    return Collections.unmodifiableList(operands);
  }

  MachineInstr addOperand(MachineOperand operand) {
    operands.add(operand);
    return this;
  }

  void setOperand(int index, MachineOperand operand) {
    operands.set(index, operand);
  }

  MachineType getType() {
    return type;
  }

  void setType(MachineType type) {
    this.type = type;
  }

  String getPredicate() {
    return predicate;
  }

  void setPredicate(String predicate) {
    this.predicate = predicate;
  }

  String getCallee() {
    return callee;
  }

  void setCallee(String callee) {
    this.callee = callee;
  }

  boolean isTerminator() {
    return opcode == MachineOpcode.BR || opcode == MachineOpcode.CONDBR || opcode == MachineOpcode.RET;
  }
}

final class MachineBasicBlock {
  private final String label;
  private final List<MachineInstr> instructions = new ArrayList<>();
  private Function sourceFunction;
  private BasicBlock sourceBlock;

  MachineBasicBlock(String label) {
    this.label = label;
  }

  String getLabel() {
    return label;
  }

  List<MachineInstr> getInstructions() {
    return instructions;
  }

  void addInstruction(MachineInstr instr) {
    instructions.add(instr);
  }

  void insertBeforeTerminator(MachineInstr instr) {
    if (!instructions.isEmpty() && instructions.get(instructions.size() - 1).isTerminator()) {
      instructions.add(instructions.size() - 1, instr);
    } else {
      instructions.add(instr);
    }
  }

  Function getSourceFunction() {
    return sourceFunction;
  }

  void setSourceFunction(Function sourceFunction) {
    this.sourceFunction = sourceFunction;
  }

  BasicBlock getSourceBlock() {
    return sourceBlock;
  }

  void setSourceBlock(BasicBlock sourceBlock) {
    this.sourceBlock = sourceBlock;
  }
}

final class MachineFunction {
  private final String name;
  private final MachineType returnType;
  private final List<MachineBasicBlock> blocks = new ArrayList<>();
  private final MachineFrameInfo frameInfo = new MachineFrameInfo();
  private final List<VirtualRegister> arguments = new ArrayList<>();
  private final List<MachineType> argumentTypes = new ArrayList<>();
  private int nextVRegId = 0;

  MachineFunction(String name, MachineType returnType) {
    this.name = name;
    this.returnType = returnType;
  }

  String getName() {
    return name;
  }

  MachineType getReturnType() {
    return returnType;
  }

  MachineFrameInfo getFrameInfo() {
    return frameInfo;
  }

  VirtualRegister createVirtualRegister(MachineType type, String hint) {
    return new VirtualRegister(nextVRegId++, type, hint);
  }

  void addArgument(VirtualRegister reg, MachineType type) {
    arguments.add(reg);
    argumentTypes.add(type);
  }

  List<VirtualRegister> getArguments() {
    return Collections.unmodifiableList(arguments);
  }

  List<MachineType> getArgumentTypes() {
    return Collections.unmodifiableList(argumentTypes);
  }

  MachineBasicBlock addBlock(String label) {
    MachineBasicBlock block = new MachineBasicBlock(label);
    blocks.add(block);
    return block;
  }

  List<MachineBasicBlock> getBlocks() {
    return Collections.unmodifiableList(blocks);
  }

  MachineBasicBlock getEntryBlock() {
    return blocks.isEmpty() ? null : blocks.get(0);
  }
}

final class MachineModule {
  private final accela.ir.Module sourceModule;
  private final List<MachineFunction> functions = new ArrayList<>();
  private final Map<Function, MachineFunction> bySourceFunction = new LinkedHashMap<>();
  private final Map<GlobalVariable, String> globalSymbols = new LinkedHashMap<>();

  MachineModule(accela.ir.Module sourceModule) {
    this.sourceModule = sourceModule;
    for (GlobalVariable global : sourceModule.getGlobals()) {
      globalSymbols.put(global, global.getName());
    }
  }

  accela.ir.Module getSourceModule() {
    return sourceModule;
  }

  void addFunction(Function source, MachineFunction machineFunction) {
    functions.add(machineFunction);
    bySourceFunction.put(source, machineFunction);
  }

  List<MachineFunction> getFunctions() {
    return Collections.unmodifiableList(functions);
  }

  String getGlobalSymbol(GlobalVariable global) {
    return globalSymbols.get(global);
  }
}
