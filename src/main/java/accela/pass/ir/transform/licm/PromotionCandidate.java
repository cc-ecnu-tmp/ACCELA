package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** A scalar object whose memory traffic LICM can replace with loop-carried SSA values. */
record PromotionCandidate(Value pointer, Type valueType, List<ExitEdge> exitEdges) {
  record ExitEdge(BasicBlock predecessor, BasicBlock exit) {}

  static List<PromotionCandidate> find(
      LoopAnalysis.Loop loop,
      GlobalModRefAnalysis.Result modRef,
      DominatorTreeAnalysis.Result dominators) {
    List<ExitEdge> exitEdges = exitEdges(loop);
    if (exitEdges.isEmpty()) return List.of();

    List<PromotionCandidate> result = new ArrayList<>();
    Set<Value> pointers = storedPointers(loop);
    for (Value pointer : pointers) {
      Type valueType = storedType(loop, pointer);
      if (valueType != null
          && isAvailableAtPreheader(pointer, loop, dominators)
          && isSafeToSpeculateLoad(pointer)
          && isSafe(loop, pointer, valueType, modRef)) {
        result.add(new PromotionCandidate(pointer, valueType, exitEdges));
      }
    }
    return result;
  }

  private static Set<Value> storedPointers(LoopAnalysis.Loop loop) {
    Set<Value> pointers = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.STORE) {
          pointers.add(instruction.getOperand(1));
        }
      }
    }
    return pointers;
  }

  private static Type storedType(LoopAnalysis.Loop loop, Value pointer) {
    Type type = null;
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() != Instruction.Opcode.STORE
            || instruction.getOperand(1) != pointer) continue;
        Type stored = instruction.getOperand(0).getType();
        if (stored.isArray() || stored.isPointer() || type != null && type != stored) return null;
        type = stored;
      }
    }
    return type;
  }

  private static boolean isAvailableAtPreheader(
      Value pointer,
      LoopAnalysis.Loop loop,
      DominatorTreeAnalysis.Result dominators) {
    return !(pointer instanceof Instruction definition)
        || !loop.contains(definition.getParent())
            && dominators.dominates(definition.getParent(), loop.preheader());
  }

  /**
   * Loading an arbitrary argument before its guarded loop access could introduce a trap.
   *
   * <p>Global/alloca roots are accepted under the IR's existing assumption that in-bounds GEPs
   * are formed by the frontend. Proving dynamic array bounds belongs in a future range analysis;
   * this check deliberately does not claim that an arbitrary pointer is dereferenceable.
   */
  private static boolean isSafeToSpeculateLoad(Value pointer) {
    Value root = PointerProvenance.root(pointer);
    return root instanceof GlobalVariable
        || root instanceof Instruction instruction
            && instruction.getOpcode() == Instruction.Opcode.ALLOCA;
  }

  private static List<ExitEdge> exitEdges(LoopAnalysis.Loop loop) {
    Set<ExitEdge> exits = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)) exits.add(new ExitEdge(block, successor));
      }
    }
    return List.copyOf(exits);
  }

  private static boolean isSafe(
      LoopAnalysis.Loop loop,
      Value pointer,
      Type valueType,
      GlobalModRefAnalysis.Result modRef) {
    int loads = 0;
    int stores = 0;
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.CALL) {
          if (modRef == null
              || modRef.mayRead(instruction, pointer)
              || modRef.mayWrite(instruction, pointer)) return false;
          continue;
        }
        int pointerIndex = pointerIndex(instruction);
        if (pointerIndex < 0) continue;
        Value accessed = instruction.getOperand(pointerIndex);
        if (accessed == pointer) {
          if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
            if (instruction.getType() != valueType) return false;
            loads++;
          }
          else stores++;
        } else if (PointerProvenance.mayAlias(accessed, pointer)) {
          return false;
        }
      }
    }
    return loads > 0 && stores > 0 && hasOnlyDirectAccesses(loop, pointer);
  }

  private static boolean hasOnlyDirectAccesses(
      LoopAnalysis.Loop loop, Value pointer) {
    return pointer.getUses().stream().allMatch(use -> {
      Instruction user = use.getUser();
      return !loop.contains(user.getParent())
          || pointerIndex(user) == use.getOperandIndex();
    });
  }

  static int pointerIndex(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case LOAD -> 0;
      case STORE -> 1;
      default -> -1;
    };
  }
}
