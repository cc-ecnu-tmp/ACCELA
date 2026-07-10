package accela.backend.target;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterLocation;
import accela.backend.regalloc.StackLocation;
import accela.backend.regalloc.ValueLocation;
import accela.ir.Constant;
import accela.ir.GlobalVariable;
import accela.ir.Type;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

public final class RISCVAsmPrinter {
  private final RISCVTarget target;
  private final RISCVFrameLowering frameLowering;
  private final RISCVAllocationRewriter allocationRewriter;

  public RISCVAsmPrinter(RISCVTarget target, RISCVFrameLowering frameLowering, RISCVAllocationRewriter allocationRewriter) {
    this.target = target;
    this.frameLowering = frameLowering;
    this.allocationRewriter = allocationRewriter;
  }

  public String print(MachineModule module, Map<MachineFunction, AllocationResult> allocations) {
    List<String> lines = new ArrayList<>();
    lines.add(".attribute arch, \"rv64gc\"");
    emitGlobals(module.getSourceModule(), lines);
    lines.add(".text");
    for (MachineFunction function : module.getFunctions()) {
      frameLowering.finalizeFrame(function);
      emitFunction(function, allocations.get(function), lines);
    }
    return String.join("\n", lines) + "\n";
  }

  private void emitGlobals(accela.ir.Module module, List<String> lines) {
    if (module.getGlobals().isEmpty()) return;
    for (GlobalVariable global : module.getGlobals()) {
      lines.add(global.isConstant() ? ".section .rodata" : ".data");
      lines.add(".globl " + global.getName());
      lines.add(global.getName() + ":");
      emitGlobalConstant(global.getInitializer(), global.getValueType(), lines);
    }
  }

  private void emitGlobalConstant(Constant constant, Type type, List<String> lines) {
    if (constant instanceof Constant.Int) {
      lines.add("  .word " + ((Constant.Int) constant).value);
      return;
    }
    if (constant instanceof Constant.Float) {
      int bits = java.lang.Float.floatToRawIntBits(((Constant.Float) constant).value);
      lines.add("  .word " + bits);
      return;
    }
    if (constant instanceof Constant.Zero) {
      lines.add("  .zero " + target.sizeOfIrType(type));
      return;
    }
    if (constant instanceof Constant.Array) {
      Constant.Array array = (Constant.Array) constant;
      Type elemType = type.innerType;
      for (Constant elem : array.elements) {
        emitGlobalConstant(elem, elemType, lines);
      }
      int declared = array.elements.size();
      int total = type.size;
      for (int i = declared; i < total; i++) {
        emitGlobalConstant(Constant.zero(elemType), elemType, lines);
      }
      return;
    }
    throw new UnsupportedOperationException("Unsupported global initializer: " + constant.getClass().getName());
  }

  private void emitFunction(MachineFunction function, AllocationResult allocation, List<String> lines) {
    lines.add("");
    lines.add(".globl " + function.getName());
    lines.add(function.getName() + ":");
    frameLowering.emitPrologue(function, lines);
    Map<VirtualRegister, Integer> useCounts = countUses(function);
    for (MachineBasicBlock block : function.getBlocks()) {
      lines.add(labelFor(function, block) + ":");
      List<MachineInstr> instructions = block.getInstructions();
      for (int i = 0; i < instructions.size(); i++) {
        MachineInstr instr = instructions.get(i);
        if (i + 1 < instructions.size()
            && canFuseCompareBranch(instr, instructions.get(i + 1), useCounts)) {
          allocationRewriter.emitCompareBranch(
              function, instr, instructions.get(++i), allocation, lines);
          continue;
        }
        allocationRewriter.emitInstruction(function, instr, allocation, lines);
      }
    }
  }

  private static Map<VirtualRegister, Integer> countUses(MachineFunction function) {
    Map<VirtualRegister, Integer> counts = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (var operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) {
            counts.merge(register.getRegister(), 1, Integer::sum);
          }
        }
      }
    }
    return counts;
  }

  private static boolean canFuseCompareBranch(
      MachineInstr compare, MachineInstr branch, Map<VirtualRegister, Integer> useCounts) {
    if (compare.getOpcode() != MachineOpcode.ICMP
        || branch.getOpcode() != MachineOpcode.CONDBR
        || !(branch.getOperands().get(0) instanceof VRegOperand condition)) return false;
    VirtualRegister result = compare.getDest();
    return condition.getRegister() == result && useCounts.getOrDefault(result, 0) == 1;
  }

  private String labelFor(MachineFunction function, MachineBasicBlock block) {
    return ".L_" + function.getName() + "_" + block.getLabel().replace('.', '_');
  }
}
