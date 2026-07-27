package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
/** A scalar object whose memory traffic LICM can replace with loop-carried SSA values. */
record PromotionCandidate(GlobalVariable global, List<BasicBlock> exits) {
  static List<PromotionCandidate> find(
      LoopAnalysis.Loop loop, GlobalModRefAnalysis.Result modRef) {
    List<BasicBlock> exits = dedicatedExits(loop);
    if (exits.isEmpty()) return List.of();

    List<PromotionCandidate> result = new ArrayList<>();
    for (GlobalVariable global : storedScalars(loop)) {
      if (isSafe(loop, global, modRef)) {
        result.add(new PromotionCandidate(global, exits));
      }
    }
    return result;
  }

  private static Set<GlobalVariable> storedScalars(LoopAnalysis.Loop loop) {
    Set<GlobalVariable> globals = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.STORE
            && instruction.getOperand(1) instanceof GlobalVariable global
            && !global.getValueType().isArray()
            && !global.getValueType().isPointer()) {
          globals.add(global);
        }
      }
    }
    return globals;
  }

  private static List<BasicBlock> dedicatedExits(LoopAnalysis.Loop loop) {
    Set<BasicBlock> exits = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)) exits.add(successor);
      }
    }
    boolean shared = exits.stream().anyMatch(exit ->
        exit.getPredecessors().stream().anyMatch(predecessor -> !loop.contains(predecessor)));
    return shared ? List.of() : List.copyOf(exits);
  }

  private static boolean isSafe(
      LoopAnalysis.Loop loop,
      GlobalVariable global,
      GlobalModRefAnalysis.Result modRef) {
    int loads = 0;
    int stores = 0;
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.CALL) {
          if (modRef == null
              || modRef.mayRead(instruction, global)
              || modRef.mayWrite(instruction, global)) return false;
          continue;
        }
        int pointerIndex = pointerIndex(instruction);
        if (pointerIndex < 0) continue;
        var pointer = instruction.getOperand(pointerIndex);
        if (pointer == global) {
          if (instruction.getOpcode() == Instruction.Opcode.LOAD) loads++;
          else stores++;
        } else if (PointerProvenance.mayAlias(pointer, global)) {
          return false;
        }
      }
    }
    return loads > 0 && stores > 0 && hasOnlyDirectAccesses(loop, global);
  }

  private static boolean hasOnlyDirectAccesses(
      LoopAnalysis.Loop loop, GlobalVariable global) {
    return global.getUses().stream().allMatch(use -> {
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
