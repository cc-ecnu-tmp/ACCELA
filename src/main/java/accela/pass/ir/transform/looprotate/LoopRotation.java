package accela.pass.ir.transform.looprotate;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.Map;

/** Rotates one canonical top-tested loop into guarded bottom-tested form. */
final class LoopRotation {
  private LoopRotation() {}

  static boolean rotate(Function function, LoopAnalysis.Loop loop) {
    LoopRotationCandidate candidate = LoopRotationCandidate.match(loop);
    return candidate != null && rotate(function, loop, candidate);
  }

  static boolean rotate(
      Function function, LoopAnalysis.Loop loop, LoopRotationCandidate candidate) {
    boolean bodyOnTrueEdge = candidate.bodyOnTrueEdge();

    Map<Value, Value> entryValues = new IdentityHashMap<>();
    Map<Value, Value> latchValues = new IdentityHashMap<>();
    for (Instruction phi : candidate.phis()) {
      entryValues.put(phi, incomingValue(phi, candidate.preheader()));
      latchValues.put(phi, incomingValue(phi, candidate.latch()));
    }

    BasicBlock guard = function.insertBlockAfter(
        candidate.header(), candidate.header().getLabel() + ".lr.ph");
    new IRBuilder(guard).createBr(candidate.body());
    candidate.branch().setOperand(bodyOnTrueEdge ? 1 : 2, guard);
    for (Instruction phi : candidate.phis()) {
      Instruction rotated = Instruction.createPhi(phi.getType());
      rotated.addOperand(entryValues.get(phi));
      rotated.addOperand(guard);
      rotated.addOperand(latchValues.get(phi));
      rotated.addOperand(candidate.latch());
      candidate.body().addInstructionToFront(rotated);
      for (var use : new ArrayList<>(phi.getUses())) {
        if (loop.contains(use.getUser().getParent())
            && use.getUser().getParent() != candidate.header()) {
          use.getUser().setOperand(use.getOperandIndex(), rotated);
        }
      }
      addLiveOutPhi(candidate, loop, phi);
      phi.clearAllOperands();
      phi.addOperand(entryValues.get(phi));
      phi.addOperand(candidate.preheader());
    }

    Map<Value, Value> rotatedValues = new IdentityHashMap<>(latchValues);
    for (Instruction test : candidate.tests()) {
      Instruction copy = test.copyWithoutOperands();
      copy.setName(null);
      for (int index = 0; index < test.getNumOperands(); index++) {
        copy.addOperand(rotatedValues.getOrDefault(test.getOperand(index), test.getOperand(index)));
      }
      candidate.latch().insertInstructionBefore(candidate.latchBranch(), copy);
      rotatedValues.put(test, copy);
    }
    addExitIncoming(candidate, rotatedValues);
    Value originalCondition = candidate.branch().getOperand(0);
    Value condition = rotatedValues.getOrDefault(originalCondition, originalCondition);
    candidate.latchBranch().eraseFromParent();
    IRBuilder builder = new IRBuilder(candidate.latch());
    if (bodyOnTrueEdge) {
      builder.createCondBr(condition, candidate.body(), candidate.exit());
    } else {
      builder.createCondBr(condition, candidate.exit(), candidate.body());
    }
    return true;
  }

  private static void addLiveOutPhi(
      LoopRotationCandidate candidate, LoopAnalysis.Loop loop, Instruction value) {
    var uses = new ArrayList<>(value.getUses()).stream()
        .filter(use -> !loop.contains(use.getUser().getParent()))
        .filter(use -> use.getUser().getOpcode() != Instruction.Opcode.PHI
            || use.getUser().getParent() != candidate.exit())
        .toList();
    if (uses.isEmpty()) return;

    Instruction exitValue = Instruction.createPhi(value.getType());
    exitValue.addOperand(value);
    exitValue.addOperand(candidate.header());
    candidate.exit().addInstructionToFront(exitValue);
    for (var use : uses) use.getUser().setOperand(use.getOperandIndex(), exitValue);
  }

  private static void addExitIncoming(
      LoopRotationCandidate candidate, Map<Value, Value> rotatedValues) {
    for (Instruction phi : candidate.exit().getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value value = incomingValue(phi, candidate.header());
      phi.addOperand(rotatedValues.getOrDefault(value, value));
      phi.addOperand(candidate.latch());
    }
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    throw new IllegalStateException("loop PHI has no incoming value");
  }
}
