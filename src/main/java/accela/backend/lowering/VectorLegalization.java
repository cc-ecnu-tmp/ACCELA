package accela.backend.lowering;

import accela.backend.machine.VectorShape;
import accela.backend.machine.MachineType;
import accela.backend.target.RISCVTarget;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.pass.ir.transform.ScalarizedVectorCleanup;
import accela.pass.ir.transform.VectorScalarization;

/**
 * Chooses the native fixed-vector path when the selected target can represent the module.
 *
 * <p>The standard psABI has no vector arguments or returns, and globals still use the scalar
 * object emitter. Such modules deliberately retain the established scalar correctness fallback.
 */
public final class VectorLegalization {
  private VectorLegalization() {}

  public static void run(accela.ir.Module module, RISCVTarget target) {
    if (!target.hasRVV() || requiresWholeModuleFallback(module)) {
      if (VectorScalarization.run(module)) ScalarizedVectorCleanup.run(module);
      return;
    }
    boolean changed = false;
    for (Function function : module.getFunctions()) {
      if (!canKeepNativeVectors(function, target)) {
        changed |= VectorScalarization.runFunction(function);
      }
    }
    if (changed) ScalarizedVectorCleanup.run(module);
  }

  private static boolean requiresWholeModuleFallback(accela.ir.Module module) {
    for (GlobalVariable global : module.getGlobals()) {
      if (containsVector(global.getValueType())) return true;
    }
    for (Function function : module.getFunctions()) {
      if (function.getReturnType().isVector()) return true;
      if (function.getArguments().stream().anyMatch(argument -> argument.getType().isVector())) {
        return true;
      }
    }
    return false;
  }

  private static boolean canKeepNativeVectors(Function function, RISCVTarget target) {
    for (var block : function.getBlocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.SMULH
            && instruction.getType().isVector()) return false;
        if ((instruction.getOpcode() == Instruction.Opcode.SITOFP
                || instruction.getOpcode() == Instruction.Opcode.FPTOSI)
            && instruction.getType().isVector()
            && MachineType.fromIr(instruction.getType().getElementType()).getSize()
                != MachineType.fromIr(instruction.getOperand(0).getType().getElementType())
                    .getSize()) {
          return false;
        }
        if (requiresUnsupportedMaskManipulation(instruction)) return false;
        if (instruction.getOpcode() == Instruction.Opcode.CALL
            && (instruction.getType().isVector()
                || java.util.stream.IntStream.range(0, instruction.getNumOperands())
                    .anyMatch(index -> instruction.getOperand(index).getType().isVector()))) {
          return false;
        }
        if (!representable(instruction.getType(), target)) return false;
        for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
          if (!representable(instruction.getOperand(operand).getType(), target)) return false;
        }
      }
    }
    return true;
  }

  private static boolean containsVector(Type type) {
    return type.isVector() || type.isArray() && containsVector(type.getElementType());
  }

  private static boolean representable(Type type, RISCVTarget target) {
    if (!type.isVector()) return true;
    try {
      VectorShape.fromIr(type, target.getMinimumVLEN());
      return true;
    } catch (IllegalArgumentException unsupported) {
      return false;
    }
  }

  private static boolean requiresUnsupportedMaskManipulation(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case BUILD_VECTOR, SPLAT, INSERT_ELEMENT, SHUFFLE_VECTOR, AND, XOR ->
          instruction.getType().isVector() && instruction.getType().getElementType() == Type.I1;
      case EXTRACT_ELEMENT ->
          instruction.getOperand(0).getType().isVector()
              && instruction.getOperand(0).getType().getElementType() == Type.I1;
      default -> false;
    };
  }
}
