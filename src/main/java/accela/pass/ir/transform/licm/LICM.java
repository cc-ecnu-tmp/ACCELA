package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Hoists loop-invariant computations and promotes loop-local scalar memory traffic. */
public final class LICM {
  private LICM() {}

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    accela.ir.Module module = function.getModule();
    GlobalModRefAnalysis.Result modRef =
        module == null ? null : GlobalModRefAnalysis.analyze(module);
    return runOnFunction(function, fam, modRef);
  }

  private static boolean runOnFunction(
      Function function,
      FunctionAnalysisManager fam,
      GlobalModRefAnalysis.Result modRef) {
    DominatorTreeAnalysis.Result dominators =
        fam.getResult(DominatorTreeAnalysis.class, function);
    boolean changed = false;
    List<LoopAnalysis.Loop> loops = fam.getResult(LoopAnalysis.class, function).loops();
    for (int loopIndex = 0; loopIndex < loops.size(); loopIndex++) {
      LoopAnalysis.Loop loop = loops.get(loopIndex);
      boolean cfgChanged = false;
      boolean dedicatedPreheader = loop.preheader() != null;
      BasicBlock preheader = dedicatedPreheader
          ? loop.preheader() : speculativePreheader(loop, loops);
      if (preheader == null || preheader.getTerminator() == null) continue;
      if (dedicatedPreheader) {
        boolean promoted = LoopAccessPromotion.run(function, loop, modRef, dominators);
        changed |= promoted;
        if (promoted) {
          dominators = new DominatorTreeAnalysis().run(function, null);
          cfgChanged = true;
        }
      }
      LoopMemory memory = collectMemoryEffects(function, loop);

      boolean localChange;
      do {
        localChange = false;
        for (BasicBlock block : function.getBlocks()) {
          if (!loop.contains(block)) continue;
          for (Instruction instruction : List.copyOf(block.getInstructions())) {
            if (!dedicatedPreheader && !isSpeculatableAddress(instruction)
                || !operandsAreAvailable(instruction, loop, preheader, dominators)
                || !isSafeToHoist(instruction, loop, memory, modRef, dominators)) continue;
            block.remove(instruction);
            preheader.insertInstructionBefore(preheader.getTerminator(), instruction);
            localChange = true;
            changed = true;
          }
        }
      } while (localChange);
      // Address expressions formed in the loop may only become promotion candidates after
      // invariant GEPs have reached the preheader.
      if (dedicatedPreheader) {
        boolean promoted = LoopAccessPromotion.run(function, loop, modRef, dominators);
        changed |= promoted;
        if (promoted) {
          dominators = new DominatorTreeAnalysis().run(function, null);
          cfgChanged = true;
        }
      }
      if (cfgChanged) {
        fam.invalidate(function, PreservedAnalyses.none());
        dominators = fam.getResult(DominatorTreeAnalysis.class, function);
        loops = fam.getResult(LoopAnalysis.class, function).loops();
        // Revisit nesting from the beginning so outer loops include newly split edges.
        loopIndex = -1;
      }
    }
    return changed;
  }

  private static BasicBlock speculativePreheader(
      LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> loops) {
    // Nested loops often enter directly from a conditional parent header.
    // That edge may speculate non-trapping address calculations, but nothing else.
    List<BasicBlock> outside = loop.header().getPredecessors().stream()
        .filter(block -> !loop.contains(block))
        .toList();
    if (outside.size() != 1) return null;
    BasicBlock candidate = outside.getFirst();
    boolean unrelated = loops.stream().anyMatch(
        other -> other.contains(candidate) && !other.contains(loop.header()));
    return unrelated ? null : candidate;
  }

  private static boolean isSpeculatableAddress(Instruction instruction) {
    return instruction.getOpcode() == Instruction.Opcode.GEP
        || instruction.getOpcode() == Instruction.Opcode.SEXT
        || instruction.getOpcode() == Instruction.Opcode.ZEXT;
  }

  private static boolean operandsAreAvailable(
      Instruction instruction,
      LoopAnalysis.Loop loop,
      BasicBlock destination,
      DominatorTreeAnalysis.Result dominators) {
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      Value operand = instruction.getOperand(index);
      if (operand instanceof Instruction definition
          && (loop.contains(definition.getParent())
              || !dominators.dominates(definition.getParent(), destination))) return false;
    }
    return true;
  }

  private static boolean isSafeToHoist(
      Instruction instruction,
      LoopAnalysis.Loop loop,
      LoopMemory memory,
      GlobalModRefAnalysis.Result modRef,
      DominatorTreeAnalysis.Result dominators) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SHL, ASHR, AND, FADD, FSUB, FMUL, FDIV, FNEG,
          ICMP, FCMP, SITOFP, FPTOSI, XOR, BUILD_VECTOR, SPLAT, EXTRACT_ELEMENT,
          INSERT_ELEMENT, SHUFFLE_VECTOR, SELECT ->
          executesOnEveryBackedge(instruction, loop, dominators);
      case GEP, ZEXT, SEXT -> true;
      case SDIV, SREM -> executesOnEveryBackedge(instruction, loop, dominators)
          && instruction.getOperand(1) instanceof Constant.Int divisor
          && divisor.value != 0;
      case LOAD -> shouldHoistLoad(instruction, loop, memory)
          && (instruction.getParent() == loop.header()
              || isScalarObject(instruction.getOperand(0)))
          && !memory.mayWrite(instruction.getOperand(0), modRef);
      // A header call is guaranteed to execute whenever the loop is entered,
      // so moving a pure call to its preheader does not speculate it.
      case CALL -> instruction.getParent() == loop.header()
          && modRef != null && modRef.isPure(instruction);
      default -> false;
    };
  }

  private static boolean executesOnEveryBackedge(
      Instruction instruction,
      LoopAnalysis.Loop loop,
      DominatorTreeAnalysis.Result dominators) {
    return loop.latches().stream()
        .allMatch(latch -> dominators.dominates(instruction.getParent(), latch));
  }

  private static boolean shouldHoistLoad(
      Instruction load, LoopAnalysis.Loop loop, LoopMemory memory) {
    // A header load already executes whenever the loop is entered. For a
    // conditional load, require repeated static accesses to pay for speculation.
    return load.getParent() == loop.header()
        || memory.loadCount(load.getOperand(0)) > 1;
  }

  private static boolean isScalarObject(Value pointer) {
    if (pointer instanceof GlobalVariable global) {
      return !global.getValueType().isArray() && !global.getValueType().isPointer();
    }
    return pointer instanceof Instruction alloca
        && alloca.getOpcode() == Instruction.Opcode.ALLOCA
        && !alloca.getAllocatedType().isArray()
        && !alloca.getAllocatedType().isPointer();
  }

  private static LoopMemory collectMemoryEffects(
      Function function, LoopAnalysis.Loop loop) {
    List<Value> stores = new ArrayList<>();
    List<Instruction> calls = new ArrayList<>();
    Map<Value, Integer> loads = new IdentityHashMap<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!loop.contains(block)) continue;
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.STORE) {
          stores.add(instruction.getOperand(1));
        } else if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
          loads.merge(instruction.getOperand(0), 1, Integer::sum);
        } else if (instruction.getOpcode() == Instruction.Opcode.CALL) {
          calls.add(instruction);
        }
      }
    }
    return new LoopMemory(stores, calls, loads);
  }

  private record LoopMemory(
      List<Value> stores, List<Instruction> calls, Map<Value, Integer> loads) {
    int loadCount(Value pointer) {
      return loads.getOrDefault(pointer, 0);
    }

    boolean mayWrite(Value pointer, GlobalModRefAnalysis.Result modRef) {
      if (stores.stream().anyMatch(store -> PointerProvenance.mayAlias(store, pointer))) {
        return true;
      }
      return calls.stream().anyMatch(call -> modRef == null || modRef.mayWrite(call, pointer));
    }
  }

  public static final class Pass implements FunctionPass {
    private accela.ir.Module cachedModule;
    private GlobalModRefAnalysis.Result modRef;

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (function.getModule() != cachedModule) {
        cachedModule = function.getModule();
        modRef = cachedModule == null ? null : GlobalModRefAnalysis.analyze(cachedModule);
      }
      if (!runOnFunction(function, fam, modRef)) return PreservedAnalyses.all();
      return PreservedAnalyses.none();
    }
  }
}
