package accela.pass.ir.transform.sroa;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Use;
import java.util.ArrayList;
import java.util.List;

/**
 * Eliminates trivial parameter copy slots in the entry block.
 *
 * <p>The frontend currently lowers each parameter to {@code alloca + store %arg -> slot} so that
 * later code can treat locals and parameters uniformly. When the slot is only ever loaded and never
 * redefined, this utility rewrites those loads back to the original function argument and removes
 * the temporary stack slot entirely.
 */
public final class PromoteParameterSlots {
  private PromoteParameterSlots() {}

  public static boolean runOnFunction(Function function) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    boolean changed = false;
    for (Instruction inst : new ArrayList<>(entry.getInstructions())) {
      if (inst.getOpcode() != Instruction.Opcode.ALLOCA) continue;
      if (promoteSingleParameterSlot(inst, entry)) changed = true;
    }
    return changed;
  }

  private static boolean promoteSingleParameterSlot(Instruction alloca, BasicBlock entry) {
    Instruction initStore = null;
    Function.Argument sourceArg = null;
    List<Instruction> loads = new ArrayList<>();

    for (Use use : new ArrayList<>(alloca.getUses())) {
      Instruction user = use.getUser();
      switch (user.getOpcode()) {
        case LOAD:
          if (use.getOperandIndex() != 0) return false;
          loads.add(user);
          break;
        case STORE:
          if (use.getOperandIndex() != 1) return false;
          if (initStore != null || user.getParent() != entry) return false;
          if (!(user.getOperand(0) instanceof Function.Argument arg)) return false;
          initStore = user;
          sourceArg = arg;
          break;
        default:
          return false;
      }
    }

    if (initStore == null || sourceArg == null) return false;

    for (Instruction load : loads) {
      load.replaceAllUsesWith(sourceArg);
      load.eraseFromParent();
    }
    initStore.eraseFromParent();
    alloca.eraseFromParent();
    return true;
  }
}
