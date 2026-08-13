package accela.pass.ir.transform.loop.fusion;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.verify.IRVerifier;
import java.util.ArrayList;
import java.util.List;

/** Transactional CFG/SSA rewrite for one fully-proved loop pair. */
final class SameDomainLoopFusionTransform {
  private SameDomainLoopFusionTransform() {}

  static void apply(
      SameDomainLoopFusionMatcher.Candidate candidate,
      SameDomainLoopFusionProfitability.Plan plan) {
    if (plan.payloadInstructions() < 0 || plan.eliminatedInstructions() < 3) {
      throw new IllegalArgumentException("invalid same-domain fusion profitability plan");
    }
    var first = candidate.first();
    var second = candidate.second();

    // Contract proven single-iteration producer/consumer temporaries before moving payload.
    for (SameDomainLoopFusionMatcher.Forwarding forwarding : candidate.forwardings()) {
      Value stored = forwarding.store().getOperand(0);
      for (Instruction load : forwarding.loads()) {
        load.replaceAllUsesWith(stored);
        load.eraseFromParent();
      }
      forwarding.store().eraseFromParent();
    }

    // The second IV becomes the first IV. This also rewrites every surviving address expression
    // and any legal exit use before the obsolete second header is detached.
    second.induction().replaceAllUsesWith(first.induction());

    // Move second-loop recurrence state into the fused header and retarget its incoming edges.
    for (SameDomainLoopFusionMatcher.Recurrence recurrence : candidate.secondRecurrences()) {
      Instruction phi = recurrence.phi();
      second.header().remove(phi);
      phi.clearAllOperands();
      for (BasicBlock predecessor : first.outsidePredecessors()) {
        phi.addOperand(recurrence.initial());
        phi.addOperand(predecessor);
      }
      phi.addOperand(recurrence.backedge());
      phi.addOperand(first.body());
      first.header().addInstructionToFront(phi);
    }

    // Preserve first(i) -> second(i) order by appending the surviving second payload immediately
    // before the first body's latch branch. Pure induction arithmetic may remain before it.
    Instruction firstBodyBranch = first.body().getTerminator();
    for (Instruction instruction : new ArrayList<>(second.body().getInstructions())) {
      if (instruction == second.nextInduction() || instruction.isTerminator()) continue;
      second.body().remove(instruction);
      first.body().insertInstructionBefore(firstBodyBranch, instruction);
    }

    // Replace the first loop's old exit through the second header with the final exit, retaining
    // the original continue edge and condition exactly.
    first.branch().setOperand(first.exitOperand(), second.exit());
    retargetExitPhis(second.exit(), second.header(), first.header());

    // Drop the obsolete second loop in use-safe order.
    second.induction().eraseFromParent();
    eraseAll(second.header());
    eraseAll(second.body());
    candidate.function().removeBlock(second.body());
    candidate.function().removeBlock(second.header());
    IRVerifier.verifyFunction(candidate.function());
  }

  private static void retargetExitPhis(
      BasicBlock exit, BasicBlock oldPredecessor, BasicBlock newPredecessor) {
    for (Instruction phi : exit.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      boolean replaced = false;
      for (int index = 1; index < phi.getNumOperands(); index += 2) {
        if (phi.getOperand(index) != oldPredecessor) continue;
        if (replaced) {
          throw new IllegalStateException("exit PHI has duplicate second-loop predecessor");
        }
        phi.setOperand(index, newPredecessor);
        replaced = true;
      }
    }
  }

  private static void eraseAll(BasicBlock block) {
    List<Instruction> instructions = new ArrayList<>(block.getInstructions());
    for (int index = instructions.size() - 1; index >= 0; index--) {
      Instruction instruction = instructions.get(index);
      if (instruction.hasUses()) {
        throw new IllegalStateException(
            "obsolete fused-loop instruction still has uses: " + instruction.getOpcode());
      }
      instruction.eraseFromParent();
    }
  }
}
