package accela.pass.ir.transform.scan;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** One-shot CFG rewrite after every semantic and profitability obligation has been proved. */
final class PrefixScanTransform {
  private PrefixScanTransform() {}

  static void apply(PrefixScanCandidate candidate) {
    validateTransaction(candidate);

    Instruction running = Instruction.createPhi(Type.INT);
    running.setName("prefix.scan.running");
    candidate.outerHeader().addInstructionToFront(running);

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(candidate.outputStore());
    Value elementIndex = candidate.outerInduction();
    if (candidate.kind() == PrefixScanCandidate.Kind.REVERSE_SUFFIX) {
      // This is intentionally emitted in the latch. N <= 0 follows the original exit edge and
      // never evaluates N - 1, so the empty-domain behavior and i32 overflow boundary are intact.
      Value lastIndex = builder.createSub(candidate.outerBound(), Constant.intConst(1));
      elementIndex = builder.createSub(lastIndex, candidate.outerInduction());
    }

    Map<Value, Value> replacements = new IdentityHashMap<>();
    replacements.put(candidate.reduction(), running);
    replacements.put(candidate.innerInduction(), elementIndex);
    Value nextRunning = cloneValue(candidate.reductionUpdate(), builder, replacements);

    // A suffix visits terms in the opposite lane order. The matcher admits only a zero-seeded
    // i32 ADD reduction, never SUB or FP. RV64GC i32 ADD implements addition modulo 2^32, whose
    // associativity and commutativity make this lane reversal bit-exact even across overflow.

    Value outputPointer = candidate.outputPointer();
    if (candidate.kind() == PrefixScanCandidate.Kind.REVERSE_SUFFIX) {
      Map<Value, Value> outputReplacements = new IdentityHashMap<>();
      outputReplacements.put(candidate.outerInduction(), elementIndex);
      outputPointer = cloneValue(candidate.outputPointer(), builder, outputReplacements);
    }
    candidate.outputStore().setOperand(0, nextRunning);
    candidate.outputStore().setOperand(1, outputPointer);

    running.addOperand(candidate.reductionStart());
    running.addOperand(candidate.outerPreheader());
    running.addOperand(nextRunning);
    running.addOperand(candidate.outerLatch());

    Instruction outerBranch = candidate.outerHeader().getTerminator();
    outerBranch.setOperand(1, candidate.outerLatch());

    if (candidate.kind() == PrefixScanCandidate.Kind.REVERSE_SUFFIX) {
      eraseDeadOutputAddress(candidate);
    }
    removeInnerLoop(candidate);
  }

  private static void validateTransaction(PrefixScanCandidate candidate) {
    Instruction outerBranch = candidate.outerHeader().getTerminator();
    if (outerBranch == null
        || outerBranch.getOpcode() != Instruction.Opcode.CONDBR
        || outerBranch.getOperand(1) != candidate.innerHeader()
        || candidate.outputStore().getOperand(0) != candidate.reduction()) {
      throw new IllegalStateException("prefix-scan candidate drifted before transformation");
    }
    validateCloneable(candidate.reductionUpdate(), candidate, new IdentityHashMap<>());
    if (candidate.kind() == PrefixScanCandidate.Kind.REVERSE_SUFFIX) {
      validateCloneable(candidate.outputPointer(), candidate, new IdentityHashMap<>());
    }
    for (BasicBlock block : candidate.innerLoop().blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        for (var use : instruction.getUses()) {
          if (candidate.innerLoop().contains(use.getUser().getParent())) continue;
          if (instruction == candidate.reduction()
              && use.getUser() == candidate.outputStore()) continue;
          throw new IllegalStateException(
              "prefix-scan inner value gained an escaping use before transformation");
        }
      }
    }
  }

  private static void validateCloneable(
      Value value,
      PrefixScanCandidate candidate,
      Map<Value, Boolean> visited) {
    if (!(value instanceof Instruction instruction)
        || instruction == candidate.reduction()
        || instruction == candidate.innerInduction()
        || !isCandidateExpression(instruction, candidate)
        || visited.put(value, true) != null) return;
    switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SHL, ASHR, AND, XOR, LOAD, GEP, SEXT, ZEXT -> {}
      default -> throw new IllegalStateException(
          "prefix-scan matcher admitted an unclonable instruction: "
              + instruction.getOpcode());
    }
    for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
      validateCloneable(instruction.getOperand(operand), candidate, visited);
    }
  }

  private static boolean isCandidateExpression(
      Instruction instruction, PrefixScanCandidate candidate) {
    return instruction == candidate.reductionUpdate()
        || candidate.termInstructions().contains(instruction)
        || candidate.outputAddressInstructions().contains(instruction);
  }

  private static Value cloneValue(
      Value value, IRBuilder builder, Map<Value, Value> replacements) {
    Value replacement = replacements.get(value);
    if (replacement != null) return replacement;
    if (!(value instanceof Instruction instruction)) return value;

    Value clone = switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SHL, ASHR, AND, XOR -> builder.createBinary(
          instruction.getOpcode(),
          cloneValue(instruction.getOperand(0), builder, replacements),
          cloneValue(instruction.getOperand(1), builder, replacements));
      case LOAD -> builder.createLoad(
          instruction.getType(),
          cloneValue(instruction.getOperand(0), builder, replacements));
      case GEP -> {
        Value[] indices = new Value[instruction.getNumOperands() - 1];
        for (int index = 1; index < instruction.getNumOperands(); index++) {
          indices[index - 1] = cloneValue(
              instruction.getOperand(index), builder, replacements);
        }
        yield builder.createGEP(
            instruction.getGepSourceType(),
            cloneValue(instruction.getOperand(0), builder, replacements),
            indices,
            instruction.isGepInbounds());
      }
      case SEXT -> builder.createSExt(
          cloneValue(instruction.getOperand(0), builder, replacements),
          instruction.getType());
      case ZEXT -> builder.createZExt(
          cloneValue(instruction.getOperand(0), builder, replacements),
          instruction.getType());
      default -> throw new IllegalStateException(
          "cannot clone prefix-scan expression opcode " + instruction.getOpcode());
    };
    replacements.put(value, clone);
    return clone;
  }

  private static void eraseDeadOutputAddress(PrefixScanCandidate candidate) {
    List<Instruction> instructions = new ArrayList<>(candidate.outerLatch().getInstructions());
    for (int index = instructions.size() - 1; index >= 0; index--) {
      Instruction instruction = instructions.get(index);
      if (candidate.outputAddressInstructions().contains(instruction)
          && !instruction.hasUses()) {
        instruction.eraseFromParent();
      }
    }
  }

  private static void removeInnerLoop(PrefixScanCandidate candidate) {
    List<BasicBlock> blocks = List.of(candidate.innerHeader(), candidate.innerBody());
    for (BasicBlock block : blocks) {
      for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
        instruction.dropAllReferences();
      }
    }
    for (BasicBlock block : blocks) {
      for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
        if (instruction.hasUses()) {
          throw new IllegalStateException(
              "prefix-scan inner instruction still has uses during committed deletion");
        }
        block.remove(instruction);
      }
      candidate.outerHeader().getParent().removeBlock(block);
    }
  }
}
