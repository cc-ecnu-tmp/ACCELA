package accela.backend.lowering;

import accela.backend.machine.FloatImmOperand;
import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.RVVConfig;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VectorConstantOperand;
import accela.backend.machine.VirtualRegister;
import accela.ir.Constant;
import java.util.ArrayList;
import java.util.List;

/** Turns aggregate vector constants into ordinary virtual-register definitions. */
public final class VectorConstantMaterialization {
  public void run(accela.backend.machine.MachineFunction function) {
    materializePhiConstantsOnIncomingEdges(function);
    for (var block : function.getBlocks()) {
      List<MachineInstr> rewritten = new ArrayList<>();
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          MachineOperand operand = instruction.getOperands().get(index);
          if (!(operand instanceof VectorConstantOperand constant)) continue;
          if (instruction.getOpcode() == MachineOpcode.VSHUFFLE && index == 2) continue;
          MaterializedConstant materialized = createMaterialization(function, constant);
          rewritten.add(materialized.instruction());
          instruction.setOperand(index, new VRegOperand(materialized.register()));
        }
        rewritten.add(instruction);
      }
      block.getInstructions().clear();
      block.getInstructions().addAll(rewritten);
    }
  }

  private void materializePhiConstantsOnIncomingEdges(
      accela.backend.machine.MachineFunction function) {
    for (MachineBasicBlock block : new ArrayList<>(function.getBlocks())) {
      for (MachineInstr instruction : new ArrayList<>(block.getInstructions())) {
        if (instruction.getOpcode() != MachineOpcode.PHI) continue;
        for (int index = 0; index < instruction.getOperands().size(); index += 2) {
          if (!(instruction.getOperands().get(index) instanceof VectorConstantOperand constant)) {
            continue;
          }
          MachineBasicBlock predecessor =
              ((BlockOperand) instruction.getOperands().get(index + 1)).getBlock();
          MaterializedConstant materialized = createMaterialization(function, constant);
          predecessor.insertBeforeTerminator(materialized.instruction());
          instruction.setOperand(index, new VRegOperand(materialized.register()));
        }
      }
    }
  }

  private MaterializedConstant createMaterialization(
      accela.backend.machine.MachineFunction function, VectorConstantOperand constant) {
    VirtualRegister register =
        function.createVectorRegister(constant.getShape(), "vector.constant");
    List<MachineOperand> elements =
        constant.getElements().stream()
            .map(VectorConstantMaterialization::lowerScalarConstant)
            .toList();
    boolean splat =
        !elements.isEmpty()
            && elements.stream().allMatch(element -> sameValue(elements.get(0), element));
    MachineInstr build =
        new MachineInstr(splat ? MachineOpcode.VSPLAT : MachineOpcode.VBUILD, register);
    build.setType(MachineType.VECTOR);
    build.setRVVConfig(RVVConfig.forShape(constant.getShape()));
    if (splat) build.addOperand(elements.get(0));
    else elements.forEach(build::addOperand);
    return new MaterializedConstant(register, build);
  }

  private static MachineOperand lowerScalarConstant(Constant constant) {
    if (constant instanceof Constant.Int integer) return new ImmOperand(integer.value);
    if (constant instanceof Constant.Float floating) return new FloatImmOperand(floating.value);
    if (constant instanceof Constant.Zero) {
      return constant.getType().isFloat() ? new FloatImmOperand(0.0f) : new ImmOperand(0);
    }
    throw new IllegalArgumentException("unsupported vector constant element: " + constant);
  }

  private static boolean sameValue(MachineOperand left, MachineOperand right) {
    if (left instanceof ImmOperand leftInteger && right instanceof ImmOperand rightInteger) {
      return leftInteger.getValue() == rightInteger.getValue();
    }
    if (left instanceof FloatImmOperand leftFloat && right instanceof FloatImmOperand rightFloat) {
      return Float.floatToRawIntBits(leftFloat.getValue())
          == Float.floatToRawIntBits(rightFloat.getValue());
    }
    return false;
  }

  private record MaterializedConstant(VirtualRegister register, MachineInstr instruction) {}
}
