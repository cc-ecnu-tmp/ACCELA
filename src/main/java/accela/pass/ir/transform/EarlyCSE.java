package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.MemoryLocation;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**

   Eliminates repeated expressions within a basic block.

   This is a simple version of GVN, but benefits are real.

   @todo: Add actual GVN.

 */
public final class EarlyCSE {
  private EarlyCSE() {}

  public static final class Pass implements FunctionPass {
    private accela.ir.Module cachedModule;
    private GlobalModRefAnalysis.Result modRef;

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (function.getModule() != cachedModule) {
        cachedModule = function.getModule();
        modRef = cachedModule == null ? null : GlobalModRefAnalysis.analyze(cachedModule);
      }
      return runOnFunction(function, modRef)
          ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  public static boolean runOnFunction(Function function) {
    accela.ir.Module module = function.getModule();
    return runOnFunction(
        function, module == null ? null : GlobalModRefAnalysis.analyze(module));
  }

  private static boolean runOnFunction(
      Function function, GlobalModRefAnalysis.Result modRef) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      changed |= runOnBlock(block, modRef);
    }
    return changed;
  }

  private static boolean runOnBlock(
      BasicBlock block, GlobalModRefAnalysis.Result modRef) {
    Map<Expression, Value> available = new HashMap<>();
    Map<MemoryLocation, Value> availableLoads = new HashMap<>();
    boolean changed = false;
    for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
      if (instruction.getOpcode() == Instruction.Opcode.STORE) {
        Value stored = instruction.getOperand(0);
        MemoryLocation storedLocation = MemoryLocation.fromInstruction(instruction);
        boolean redundant = availableLoads.get(storedLocation) == stored;

        // Equal-width, naturally aligned accesses into one object are either identical or
        // disjoint. Writing the known value therefore preserves the load fact in both cases.
        availableLoads.keySet().removeIf(loadedLocation ->
            PointerProvenance.mayAlias(loadedLocation.pointer(), storedLocation.pointer())
                && (availableLoads.get(loadedLocation) != stored
                    || !loadedLocation.hasSameAccessShape(storedLocation)
                    || mayPartiallyOverlap(loadedLocation, storedLocation)));
        if (!redundant) availableLoads.put(storedLocation, stored);
        if (redundant) {
          instruction.eraseFromParent();
          changed = true;
        }
        continue;
      }
      Value replacement = switch (instruction.getOpcode()) {
        case CALL -> {
          if (modRef == null) availableLoads.clear();
          else availableLoads.keySet().removeIf(
              location -> modRef.mayWrite(instruction, location.pointer()));
          yield modRef != null && modRef.isPure(instruction)
              ? available.putIfAbsent(expressionFor(instruction), instruction) : null;
        }
        case LOAD -> availableLoads.putIfAbsent(
            MemoryLocation.fromInstruction(instruction), instruction);
        default -> {
          if (!isSimple(instruction)) yield null;
          yield available.putIfAbsent(expressionFor(instruction), instruction);
        }
      };
      if (replacement == null) continue;
      instruction.replaceAllUsesWith(replacement);
      instruction.eraseFromParent();
      changed = true;
    }
    return changed;
  }

  private static boolean mayPartiallyOverlap(MemoryLocation left, MemoryLocation right) {
    if (left.pointer() == right.pointer()
        || !PointerProvenance.mayAlias(left.pointer(), right.pointer())) return false;
    Value root = PointerProvenance.root(left.pointer());
    if (root != PointerProvenance.root(right.pointer())
        || !(root instanceof GlobalVariable || isAlloca(root))) return true;
    return !isAlignedTo(left.pointer(), left.byteSize())
        || !isAlignedTo(right.pointer(), right.byteSize());
  }

  private static boolean isAlignedTo(Value pointer, long alignment) {
    if (pointer instanceof GlobalVariable global) {
      return MemoryLocation.byteSize(arrayLeafType(global.getValueType())) % alignment == 0;
    }
    if (isAlloca(pointer)) {
      return MemoryLocation.byteSize(arrayLeafType(((Instruction) pointer).getAllocatedType()))
          % alignment == 0;
    }
    if (!(pointer instanceof Instruction gep)
        || gep.getOpcode() != Instruction.Opcode.GEP
        || !isAlignedTo(gep.getOperand(0), alignment)) return false;
    for (int operand = 1; operand < gep.getNumOperands(); operand++) {
      if (gepStride(gep.getGepSourceType(), operand) % alignment != 0) return false;
    }
    return true;
  }

  private static long gepStride(Type sourceType, int operandIndex) {
    Type type = sourceType;
    for (int index = 1; index < operandIndex; index++) {
      if (type.isArray()) type = type.innerType;
    }
    return MemoryLocation.byteSize(type);
  }

  private static Type arrayLeafType(Type type) {
    while (type.isArray()) type = type.innerType;
    return type;
  }

  private static boolean isAlloca(Value value) {
    return value instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.ALLOCA;
  }

  private static boolean isSimple(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, FADD, FSUB, FMUL, FDIV, FNEG,
          ICMP, FCMP, GEP, ZEXT, SEXT, SITOFP, FPTOSI, XOR,
          BUILD_VECTOR, SPLAT, EXTRACT_ELEMENT, INSERT_ELEMENT, SHUFFLE_VECTOR, SELECT -> true;
      default -> false;
    };
  }

  private static Expression expressionFor(Instruction instruction) {
    List<Object> operands = new ArrayList<>();
    for (int i = 0; i < instruction.getNumOperands(); i++) {
      operands.add(valueKey(instruction.getOperand(i)));
    }
    String detail = switch (instruction.getOpcode()) {
      case ICMP, FCMP -> instruction.getPredicate();
      case CALL -> instruction.getCallee().getName();
      case GEP -> instruction.getGepSourceType() + ":" + instruction.isGepInbounds();
      default -> "";
    };
    return new Expression(
        instruction.getOpcode(), instruction.getType().toString(), detail, List.copyOf(operands));
  }

  private static Object valueKey(Value value) {
    if (value instanceof Constant.Int integer) {
      return new IntegerKey(integer.getType().dataType, integer.value);
    }
    if (value instanceof Constant.Float floating) {
      return new FloatKey(Float.floatToRawIntBits(floating.value));
    }
    if (value instanceof Constant.Zero zero) {
      return new ZeroKey(zero.getType().toString());
    }
    if (value instanceof Constant.Vector vector) {
      return new VectorKey(
          vector.getType().toString(), vector.elements.stream().map(EarlyCSE::valueKey).toList());
    }
    return value;
  }

  private record Expression(
      Instruction.Opcode opcode, String type, String detail, List<Object> operands) {}

  private record IntegerKey(Type.DataType type, long value) {}

  private record FloatKey(int bits) {}

  private record ZeroKey(String type) {}

  private record VectorKey(String type, List<Object> elements) {}
}
