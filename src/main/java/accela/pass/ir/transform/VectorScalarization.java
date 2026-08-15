package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.verify.IRVerifier;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/**
 * Legalizes fixed-width vector IR to the existing scalar backend instruction set.
 *
 * <p>This is deliberately a target-independent correctness fallback. It preserves vector values
 * through the mid-end, then expands construction, lane-wise arithmetic/comparisons/conversions,
 * lane manipulation, PHIs, and contiguous vector loads/stores immediately before machine
 * lowering. Direct vector allocas and globals are split into independent scalar storage lanes;
 * vector function ABIs remain explicit errors until the contest ABI is known.
 */
public final class VectorScalarization {
  private VectorScalarization() {}

  /** Scalarizes every function in {@code module}; returns whether the module changed. */
  public static boolean run(accela.ir.Module module) {
    if (!containsVectorIR(module)) return false;

    IRVerifier.verifyModule(module);
    rejectUnspecifiedFunctionABI(module);
    Map<Value, List<Value>> storageLanes = scalarizeVectorGlobals(module);
    boolean changed = !storageLanes.isEmpty();
    for (Function function : module.getFunctions()) {
      changed |= scalarizeFunction(function, storageLanes);
    }
    for (Value removedStorage : storageLanes.keySet()) {
      if (removedStorage.hasUses()) {
        throw new UnsupportedOperationException(
            "vector storage escapes scalarization: " + removedStorage.getName());
      }
    }
    IRVerifier.verifyModule(module);
    return changed;
  }

  private static boolean scalarizeFunction(
      Function function, Map<Value, List<Value>> storageLanes) {
    if (containsVector(function.getReturnType())) {
      throw unsupported("vector return ABI", function);
    }
    for (Function.Argument argument : function.getArguments()) {
      if (containsVector(argument.getType())) throw unsupported("vector argument ABI", function);
    }

    Map<Value, List<Value>> lanes = new IdentityHashMap<>();
    List<Instruction> vectorPhis = new ArrayList<>();
    List<Instruction> erase = new ArrayList<>();
    boolean changed = false;

    // Make every scalar PHI first so backedge and mutually-referential vector PHIs can resolve in a
    // second phase after all ordinary vector definitions have been expanded.
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : List.copyOf(block.getInstructions())) {
        if (instruction.getOpcode() != Opcode.PHI || !instruction.getType().isVector()) continue;
        IRBuilder builder = new IRBuilder();
        builder.setInsertPointBefore(instruction);
        List<Value> scalarPhis = new ArrayList<>();
        for (int lane = 0; lane < instruction.getType().getLaneCount(); lane++) {
          scalarPhis.add(builder.createPhi(instruction.getType().getElementType()));
        }
        lanes.put(instruction, List.copyOf(scalarPhis));
        vectorPhis.add(instruction);
        erase.add(instruction);
        changed = true;
      }
    }

    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : List.copyOf(block.getInstructions())) {
        if (vectorPhis.contains(instruction)) continue;
        IRBuilder builder = new IRBuilder();
        builder.setInsertPointBefore(instruction);

        if (instruction.getOpcode() == Opcode.ALLOCA
            && instruction.getAllocatedType() != null
            && instruction.getAllocatedType().isVector()) {
          Type vectorType = instruction.getAllocatedType();
          List<Value> scalarAllocas = new ArrayList<>();
          for (int lane = 0; lane < vectorType.getLaneCount(); lane++) {
            scalarAllocas.add(builder.createAlloca(vectorType.getElementType()));
          }
          storageLanes.put(instruction, List.copyOf(scalarAllocas));
          erase.add(instruction);
          changed = true;
          continue;
        }
        if (instruction.getOpcode() == Opcode.GEP
            && instruction.getGepSourceType() != null
            && containsVector(instruction.getGepSourceType())) {
          instruction.setGepSourceType(legalizedStorageType(instruction.getGepSourceType()));
          changed = true;
        }

        if (instruction.getOpcode() == Opcode.STORE
            && instruction.getOperand(0).getType().isVector()) {
          lowerVectorStore(
              builder, instruction, getLanes(instruction.getOperand(0), lanes), storageLanes);
          erase.add(instruction);
          changed = true;
          continue;
        }

        if (instruction.getType().isVector()) {
          List<Value> result =
              lowerVectorInstruction(builder, instruction, lanes, storageLanes, function);
          lanes.put(instruction, List.copyOf(result));
          erase.add(instruction);
          changed = true;
          continue;
        }

        if (instruction.getOpcode() == Opcode.EXTRACT_ELEMENT) {
          Value replacement = lowerExtract(
              builder,
              instruction,
              getLanes(instruction.getOperand(0), lanes),
              function);
          instruction.replaceAllUsesWith(replacement);
          erase.add(instruction);
          changed = true;
          continue;
        }

        for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
          if (instruction.getOperand(operand).getType().isVector()) {
            throw unsupported(
                "vector operand to " + instruction.getOpcode() + " after scalarization",
                function);
          }
        }
      }
    }

    for (Instruction vectorPhi : vectorPhis) {
      List<Value> scalarPhis = lanes.get(vectorPhi);
      for (int incoming = 0; incoming < vectorPhi.getNumOperands(); incoming += 2) {
        List<Value> incomingLanes = getLanes(vectorPhi.getOperand(incoming), lanes);
        Value incomingBlock = vectorPhi.getOperand(incoming + 1);
        for (int lane = 0; lane < scalarPhis.size(); lane++) {
          ((Instruction) scalarPhis.get(lane)).addOperand(incomingLanes.get(lane));
          ((Instruction) scalarPhis.get(lane)).addOperand(incomingBlock);
        }
      }
    }

    // Consumers are normally later than producers, so erase in reverse order to avoid leaving
    // transient uses pointing at detached definitions.
    Collections.reverse(erase);
    for (Instruction instruction : erase) instruction.eraseFromParent();
    return changed;
  }

  private static List<Value> lowerVectorInstruction(
      IRBuilder builder,
      Instruction instruction,
      Map<Value, List<Value>> lanes,
      Map<Value, List<Value>> storageLanes,
      Function function) {
    Type vectorType = instruction.getType();
    return switch (instruction.getOpcode()) {
      case BUILD_VECTOR -> operands(instruction);
      case SPLAT -> Collections.nCopies(vectorType.getLaneCount(), instruction.getOperand(0));
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR,
          FADD, FSUB, FMUL, FDIV ->
          lowerBinary(builder, instruction, lanes);
      case FNEG -> lowerFNeg(builder, instruction, lanes);
      case ICMP, FCMP -> lowerCompare(builder, instruction, lanes);
      case ZEXT, SEXT, SITOFP, FPTOSI -> lowerConversion(builder, instruction, lanes);
      case LOAD -> lowerVectorLoad(builder, instruction, storageLanes);
      case INSERT_ELEMENT -> lowerInsert(builder, instruction, lanes, function);
      case SHUFFLE_VECTOR -> lowerShuffle(instruction, lanes, function);
      case CALL -> throw unsupported("vector call result ABI", function);
      default -> throw unsupported(
          "vector result from unsupported opcode " + instruction.getOpcode(), function);
    };
  }

  private static List<Value> lowerBinary(
      IRBuilder builder, Instruction instruction, Map<Value, List<Value>> lanes) {
    List<Value> left = getLanes(instruction.getOperand(0), lanes);
    List<Value> right = getLanes(instruction.getOperand(1), lanes);
    List<Value> result = new ArrayList<>();
    for (int lane = 0; lane < left.size(); lane++) {
      result.add(builder.createBinary(instruction.getOpcode(), left.get(lane), right.get(lane)));
    }
    return result;
  }

  private static List<Value> lowerFNeg(
      IRBuilder builder, Instruction instruction, Map<Value, List<Value>> lanes) {
    List<Value> result = new ArrayList<>();
    for (Value lane : getLanes(instruction.getOperand(0), lanes)) {
      result.add(builder.createFNeg(lane));
    }
    return result;
  }

  private static List<Value> lowerCompare(
      IRBuilder builder, Instruction instruction, Map<Value, List<Value>> lanes) {
    List<Value> left = getLanes(instruction.getOperand(0), lanes);
    List<Value> right = getLanes(instruction.getOperand(1), lanes);
    List<Value> result = new ArrayList<>();
    for (int lane = 0; lane < left.size(); lane++) {
      result.add(instruction.getOpcode() == Opcode.ICMP
          ? builder.createICmp(instruction.getPredicate(), left.get(lane), right.get(lane))
          : builder.createFCmp(instruction.getPredicate(), left.get(lane), right.get(lane)));
    }
    return result;
  }

  private static List<Value> lowerConversion(
      IRBuilder builder, Instruction instruction, Map<Value, List<Value>> lanes) {
    List<Value> result = new ArrayList<>();
    Type destination = instruction.getType().getElementType();
    for (Value lane : getLanes(instruction.getOperand(0), lanes)) {
      result.add(switch (instruction.getOpcode()) {
        case ZEXT -> builder.createZExt(lane, destination);
        case SEXT -> builder.createSExt(lane, destination);
        case SITOFP -> builder.createSIToFP(lane, destination);
        case FPTOSI -> builder.createFPToSI(lane, destination);
        default -> throw new IllegalStateException("not a vector conversion");
      });
    }
    return result;
  }

  private static List<Value> lowerVectorLoad(
      IRBuilder builder, Instruction load, Map<Value, List<Value>> storageLanes) {
    Type vectorType = load.getType();
    Type elementType = vectorType.getElementType();
    Value pointer = load.getOperand(0);
    List<Value> directLanes = storageLanes.get(pointer);
    List<Value> result = new ArrayList<>();
    for (int lane = 0; lane < vectorType.getLaneCount(); lane++) {
      Value lanePointer = directLanes == null
          ? pointerAtLane(builder, pointer, elementType, lane) : directLanes.get(lane);
      result.add(builder.createLoad(elementType, lanePointer));
    }
    return result;
  }

  private static void lowerVectorStore(
      IRBuilder builder,
      Instruction store,
      List<Value> storedLanes,
      Map<Value, List<Value>> storageLanes) {
    Type elementType = store.getOperand(0).getType().getElementType();
    Value pointer = store.getOperand(1);
    List<Value> directLanes = storageLanes.get(pointer);
    for (int lane = 0; lane < storedLanes.size(); lane++) {
      Value lanePointer = directLanes == null
          ? pointerAtLane(builder, pointer, elementType, lane) : directLanes.get(lane);
      builder.createStore(storedLanes.get(lane), lanePointer);
    }
  }

  private static Value lowerExtract(
      IRBuilder builder, Instruction extract, List<Value> vectorLanes, Function function) {
    Value index = extract.getOperand(1);
    if (index instanceof Constant.Int constant) {
      int lane = checkedLane(constant.value, vectorLanes.size(), function, "extract");
      return vectorLanes.get(lane);
    }
    Value selected = vectorLanes.getFirst();
    for (int lane = 1; lane < vectorLanes.size(); lane++) {
      Value matches = builder.createICmp("eq", index, Constant.intConst(lane));
      selected = builder.createSelect(matches, vectorLanes.get(lane), selected);
    }
    return selected;
  }

  private static List<Value> lowerInsert(
      IRBuilder builder,
      Instruction insert,
      Map<Value, List<Value>> lanes,
      Function function) {
    List<Value> original = new ArrayList<>(getLanes(insert.getOperand(0), lanes));
    Value element = insert.getOperand(1);
    Value index = insert.getOperand(2);
    if (index instanceof Constant.Int constant) {
      original.set(checkedLane(constant.value, original.size(), function, "insert"), element);
      return original;
    }

    List<Value> result = new ArrayList<>();
    for (int lane = 0; lane < original.size(); lane++) {
      Value matches = builder.createICmp("eq", index, Constant.intConst(lane));
      result.add(builder.createSelect(matches, element, original.get(lane)));
    }
    return result;
  }

  private static List<Value> lowerShuffle(
      Instruction shuffle, Map<Value, List<Value>> lanes, Function function) {
    List<Value> choices = new ArrayList<>(getLanes(shuffle.getOperand(0), lanes));
    choices.addAll(getLanes(shuffle.getOperand(1), lanes));
    Constant.Vector mask = (Constant.Vector) shuffle.getOperand(2);
    List<Value> result = new ArrayList<>();
    for (Constant maskElement : mask.elements) {
      if (!(maskElement instanceof Constant.Int index)) {
        throw unsupported("non-integer shuffle mask", function);
      }
      result.add(choices.get(checkedLane(index.value, choices.size(), function, "shuffle")));
    }
    return result;
  }

  private static Value pointerAtLane(
      IRBuilder builder, Value base, Type elementType, int lane) {
    if (lane == 0) return base;
    return builder.createGEP(
        elementType, base, new Value[] {Constant.int64Const(lane)}, true);
  }

  private static List<Value> getLanes(Value value, Map<Value, List<Value>> lanes) {
    if (value instanceof Constant.Vector vector) return List.copyOf(vector.elements);
    if (value instanceof Constant.Zero zero && zero.getType().isVector()) {
      return Collections.nCopies(
          zero.getType().getLaneCount(), scalarZero(zero.getType().getElementType()));
    }
    List<Value> result = lanes.get(value);
    if (result == null) {
      throw new IllegalStateException("vector definition was not scalarized before use: " + value);
    }
    return result;
  }

  private static Constant scalarZero(Type type) {
    if (type == Type.FLOAT) return Constant.floatConst(0.0f);
    if (type == Type.I1) return Constant.boolConst(false);
    if (type == Type.I64) return Constant.int64Const(0);
    return Constant.intConst(0);
  }

  private static List<Value> operands(Instruction instruction) {
    List<Value> result = new ArrayList<>();
    for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
      result.add(instruction.getOperand(operand));
    }
    return result;
  }

  private static Type legalizedStorageType(Type type) {
    if (type.isVector()) {
      return Type.array(type.getElementType(), type.getLaneCount());
    }
    if (type.isArray()) {
      return Type.array(legalizedStorageType(type.getElementType()), type.size);
    }
    return type;
  }

  /** Splits a direct vector global into one ordinary scalar global per lane. */
  private static Map<Value, List<Value>> scalarizeVectorGlobals(accela.ir.Module module) {
    Map<Value, List<Value>> result = new IdentityHashMap<>();
    for (GlobalVariable global : List.copyOf(module.getGlobals())) {
      Type vectorType = global.getValueType();
      if (!vectorType.isVector()) {
        if (containsVector(vectorType)) {
          Type storageType = legalizedStorageType(vectorType);
          Constant initializer = legalizedInitializer(
              global.getInitializer(), vectorType, storageType);
          GlobalVariable replacement = new GlobalVariable(
              global.getName(), storageType, initializer, global.isConstant());
          global.replaceAllUsesWith(replacement);
          module.removeGlobal(global);
          module.addGlobal(replacement);
        }
        continue;
      }
      List<Constant> initializers = globalInitializerLanes(global, vectorType);
      List<Value> laneGlobals = new ArrayList<>();
      for (int lane = 0; lane < vectorType.getLaneCount(); lane++) {
        GlobalVariable laneGlobal = new GlobalVariable(
            global.getName() + ".lane." + lane,
            vectorType.getElementType(),
            initializers.get(lane),
            global.isConstant());
        module.addGlobal(laneGlobal);
        laneGlobals.add(laneGlobal);
      }
      module.removeGlobal(global);
      result.put(global, List.copyOf(laneGlobals));
    }
    return result;
  }

  private static Constant legalizedInitializer(
      Constant constant, Type sourceType, Type storageType) {
    if (constant instanceof Constant.Zero) return Constant.zero(storageType);
    if (sourceType.isVector() && constant instanceof Constant.Vector vector) {
      return Constant.array(storageType, vector.elements);
    }
    if (sourceType.isArray() && constant instanceof Constant.Array array) {
      List<Constant> elements = new ArrayList<>();
      for (Constant element : array.elements) {
        elements.add(legalizedInitializer(
            element, sourceType.getElementType(), storageType.getElementType()));
      }
      return Constant.array(storageType, elements);
    }
    throw new UnsupportedOperationException(
        "invalid nested vector global initializer for " + sourceType);
  }

  private static List<Constant> globalInitializerLanes(
      GlobalVariable global, Type vectorType) {
    if (global.getInitializer() instanceof Constant.Zero) {
      return Collections.nCopies(
          vectorType.getLaneCount(), scalarZero(vectorType.getElementType()));
    }
    if (global.getInitializer() instanceof Constant.Vector vector
        && vector.elements.size() == vectorType.getLaneCount()) {
      return List.copyOf(vector.elements);
    }
    throw new UnsupportedOperationException(
        "invalid vector global initializer: @" + global.getName());
  }

  private static int checkedLane(
      long index, int laneCount, Function function, String operation) {
    if (index < 0 || index >= laneCount) {
      throw unsupported(operation + " lane index " + index + " is out of range", function);
    }
    return (int) index;
  }

  private static boolean containsVectorIR(accela.ir.Module module) {
    for (accela.ir.GlobalVariable global : module.getGlobals()) {
      if (containsVector(global.getValueType())) return true;
    }
    for (Function declaration : module.getDeclares()) {
      if (containsVectorSignature(declaration)) return true;
    }
    for (Function function : module.getFunctions()) {
      if (containsVectorSignature(function)) return true;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (containsVector(instruction.getType())
              || containsVector(instruction.getAllocatedType())
              || containsVector(instruction.getGepSourceType())) return true;
          for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
            if (containsVector(instruction.getOperand(operand).getType())) return true;
          }
        }
      }
    }
    return false;
  }

  private static boolean containsVectorSignature(Function function) {
    if (containsVector(function.getReturnType())) return true;
    for (Function.Argument argument : function.getArguments()) {
      if (containsVector(argument.getType())) return true;
    }
    return false;
  }

  private static boolean containsVector(Type type) {
    if (type == null) return false;
    if (type.isVector()) return true;
    return type.isArray() && containsVector(type.getElementType());
  }

  private static void rejectUnspecifiedFunctionABI(accela.ir.Module module) {
    for (Function declaration : module.getDeclares()) {
      if (containsVectorSignature(declaration)) {
        throw new UnsupportedOperationException(
            "vector external-call ABI is not defined yet: @" + declaration.getName());
      }
    }
  }

  private static UnsupportedOperationException unsupported(String feature, Function function) {
    return new UnsupportedOperationException(feature + " in @" + function.getName());
  }
}
