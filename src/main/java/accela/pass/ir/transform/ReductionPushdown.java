package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;

/** Pushes an invariant integer factor out of an additive reduction. */
public final class ReductionPushdown {
  private ReductionPushdown() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    List<LoopAnalysis.Loop> loops = new ArrayList<>(
        fam.getResult(LoopAnalysis.class, function).loops());
    for (LoopAnalysis.Loop loop : loops) {
      Candidate candidate = Candidate.match(loop);
      if (candidate == null) continue;
      apply(candidate);
      changed = true;
    }
    return changed;
  }

  private static void apply(Candidate candidate) {
    Instruction add = candidate.update;
    int productOperand = add.getOperand(0) == candidate.product ? 0 : 1;
    add.setOperand(productOperand, candidate.variant);

    IRBuilder builder = new IRBuilder();
    Instruction insertionPoint = firstNonPhi(candidate.exit);
    if (insertionPoint == null) {
      builder.setInsertPoint(candidate.exit);
    } else {
      builder.setInsertPointBefore(insertionPoint);
    }
    Value scaled = builder.createMul(candidate.reduction, candidate.factor);

    for (Use use : List.copyOf(candidate.reduction.getUses())) {
      Instruction user = use.getUser();
      if (user == scaled || candidate.loop.contains(user.getParent())) continue;
      user.setOperand(use.getOperandIndex(), scaled);
    }
    if (!candidate.product.hasUses()) candidate.product.eraseFromParent();
  }

  private static Instruction firstNonPhi(BasicBlock block) {
    return block.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() != Instruction.Opcode.PHI)
        .findFirst().orElse(null);
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return ReductionPushdown.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }

  private record Candidate(
      LoopAnalysis.Loop loop,
      BasicBlock exit,
      Instruction reduction,
      Instruction update,
      Instruction product,
      Value factor,
      Value variant) {
    private static Candidate match(LoopAnalysis.Loop loop) {
      if (loop.preheader() == null || loop.latches().size() != 1) return null;
      BasicBlock header = loop.header();
      BasicBlock latch = loop.latches().iterator().next();
      Instruction branch = header.getTerminator();
      if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR) return null;
      List<BasicBlock> exits = header.getSuccessors().stream()
          .filter(successor -> !loop.contains(successor)).toList();
      if (exits.size() != 1 || exits.getFirst().getPredecessors().size() != 1) return null;

      for (Instruction phi : header.getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value start = incomingValue(phi, loop.preheader());
        Value backedge = incomingValue(phi, latch);
        if (!isZero(start) || !(backedge instanceof Instruction update)
            || update.getOpcode() != Instruction.Opcode.ADD
            || update.getParent() != latch) continue;
        Instruction product = productOperand(update, phi);
        if (product == null || hasUsesOtherThan(product, update)) continue;
        Factorization factorization = factorization(product, loop);
        if (factorization == null || dependsOn(factorization.variant, phi)) continue;
        List<Use> externalUses = phi.getUses().stream()
            .filter(use -> !loop.contains(use.getUser().getParent())).toList();
        if (externalUses.isEmpty()
            || externalUses.stream().anyMatch(use -> use.getUser().getParent() != exits.getFirst()
                || use.getUser().getOpcode() == Instruction.Opcode.PHI)) continue;
        return new Candidate(
            loop, exits.getFirst(), phi, update, product,
            factorization.factor, factorization.variant);
      }
      return null;
    }

    private static Instruction productOperand(Instruction add, Instruction reduction) {
      Value other;
      if (add.getOperand(0) == reduction) other = add.getOperand(1);
      else if (add.getOperand(1) == reduction) other = add.getOperand(0);
      else return null;
      return other instanceof Instruction instruction
              && instruction.getOpcode() == Instruction.Opcode.MUL
          ? instruction : null;
    }

    private static Factorization factorization(
        Instruction product, LoopAnalysis.Loop loop) {
      Value left = product.getOperand(0);
      Value right = product.getOperand(1);
      boolean leftInvariant = isInvariant(left, loop);
      boolean rightInvariant = isInvariant(right, loop);
      if (leftInvariant == rightInvariant) return null;
      return leftInvariant ? new Factorization(left, right) : new Factorization(right, left);
    }

    private static boolean isInvariant(Value value, LoopAnalysis.Loop loop) {
      return !(value instanceof Instruction instruction)
          || !loop.contains(instruction.getParent());
    }

    private static boolean dependsOn(Value value, Value dependency) {
      Set<Value> visited = Collections.newSetFromMap(new IdentityHashMap<>());
      return dependsOn(value, dependency, visited);
    }

    private static boolean dependsOn(Value value, Value dependency, Set<Value> visited) {
      if (value == dependency) return true;
      if (!(value instanceof Instruction instruction) || !visited.add(value)) return false;
      for (int index = 0; index < instruction.getNumOperands(); index++) {
        if (dependsOn(instruction.getOperand(index), dependency, visited)) return true;
      }
      return false;
    }

    private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
      for (int index = 0; index < phi.getNumOperands(); index += 2) {
        if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
      }
      return null;
    }

    private static boolean isZero(Value value) {
      if (value instanceof Constant.Zero) return true;
      if (value instanceof Constant.Int integer) return integer.value == 0;
      return value instanceof Constant.Vector vector
          && vector.elements.stream().allMatch(Candidate::isZero);
    }

    private static boolean hasUsesOtherThan(Value value, Instruction user) {
      return value.getUses().stream().anyMatch(use -> use.getUser() != user);
    }
  }

  private record Factorization(Value factor, Value variant) {}
}
