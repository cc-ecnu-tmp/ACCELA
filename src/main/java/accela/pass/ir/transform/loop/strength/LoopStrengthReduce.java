package accela.pass.ir.transform.loop.strength;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Replaces affine memory addresses with loop-carried pointer recurrences. */
public final class LoopStrengthReduce {
  private static final int MAX_RECURRENCES_PER_LOOP = 8;
  private static final int MAX_ROTATED_RECURRENCES = 2;

  private LoopStrengthReduce() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    // Row-major nested address recurrences are part of the production LSR pass after the
    // formal candidate evaluation. Run them first so the ordinary induction pass sees the
    // same preheader/latch shape that the screened implementation used.
    boolean changed = runNested(function, fam);
    if (changed) fam.invalidate(function, PreservedAnalyses.none());
    return runWithInductions(
        function, fam, fam.getResult(InductionVariableAnalysis.class, function).inductions(), false)
        || changed;
  }

  /** Visits inductions whose preheader is another loop and whose addresses are row-major. */
  private static boolean runNested(Function function, FunctionAnalysisManager fam) {
    List<InductionVariableAnalysis.Induction> nested = fam
        .getResult(InductionVariableAnalysis.class, function).allInductions().stream()
        .filter(induction -> induction.predecessor() != induction.loop().header()
            && isNested(induction, fam.getResult(LoopAnalysis.class, function).loops()))
        .toList();
    return runWithInductions(
        function, fam, nested, true);
  }

  private static boolean runWithInductions(
      Function function,
      FunctionAnalysisManager fam,
      List<InductionVariableAnalysis.Induction> inductions,
      boolean nestedOnly) {
    Map<LoopAnalysis.Loop, Integer> transformed = new IdentityHashMap<>();
    Map<LoopAnalysis.Loop, Integer> nested = new IdentityHashMap<>();
    boolean changed = false;
    for (var induction : inductions) {
      LoopAnalysis.Loop loop = induction.loop();
      if (containsCall(loop)
          || induction.phi().getNumOperands() != 4
          || loop.header().getPredecessors().size() != 2
          || (nestedOnly && induction.step() <= 0)) continue;
      Map<Object, PointerRecurrence.Result> recurrences = new HashMap<>();
      List<AddressStream> addressStreams = new ArrayList<>();
      PointerRecurrence.Result exitRecurrence = null;
      for (BasicBlock block : function.getBlocks()) {
        if (!loop.contains(block)) continue;
        for (Instruction gep : List.copyOf(block.getInstructions())) {
          int varyingIndex = candidateIndex(gep, induction);
          if (varyingIndex < 0) continue;
          if (nestedOnly && !isRowMajorNestedAddress(gep, varyingIndex)) continue;
          Object key = expressionKey(gep, induction.phi());
          PointerRecurrence.Result existing = recurrences.get(key);
          if (existing != null) {
            gep.replaceAllUsesWith(existing.pointer());
            gep.eraseFromParent();
            changed = true;
            continue;
          }
          AddressFormula formula =
              gep.isGepInbounds() && hasOneDirectMemoryUse(gep)
                  ? AddressFormula.match(gep) : null;
          OffsetMatch offset = findOffset(formula, addressStreams);
          if (offset != null) {
            PointerRecurrence.rewriteOffset(gep, offset.recurrence().pointer(), offset.bytes());
            changed = true;
            continue;
          }
          boolean crossesInnerRecurrence = !AffineGep.isDirectMemoryAddress(gep);
          if (crossesInnerRecurrence && nested.getOrDefault(loop, 0) >= 1) continue;
          if (transformed.getOrDefault(loop, 0) >= MAX_RECURRENCES_PER_LOOP) continue;
          long byteStep;
          try {
            byteStep = Math.multiplyExact(
                induction.step(), AffineGep.byteStride(gep, varyingIndex));
          } catch (ArithmeticException overflow) {
            continue;
          }
          if (byteStep % 4 != 0) continue;
          PointerRecurrence.Result recurrence =
              PointerRecurrence.rewrite(gep, varyingIndex, byteStep / 4, induction);
          recurrences.put(key, recurrence);
          if (formula != null) addressStreams.add(new AddressStream(formula, recurrence));
          if (recurrence.elementStep() == 1) exitRecurrence = recurrence;
          transformed.merge(loop, 1, Integer::sum);
          if (crossesInnerRecurrence) nested.merge(loop, 1, Integer::sum);
          changed = true;
        }
      }
      if (exitRecurrence != null) {
        PointerExitCondition.rewrite(induction, exitRecurrence);
      }
    }
    return changed;
  }

  private static boolean isNested(
      InductionVariableAnalysis.Induction induction,
      List<LoopAnalysis.Loop> loops) {
    return loops.stream().anyMatch(outer -> outer != induction.loop()
        && outer.contains(induction.predecessor())
        && outer.contains(induction.loop().header())
        && outer.blocks().size() > induction.loop().blocks().size());
  }

  private static boolean isRowMajorNestedAddress(Instruction gep, int varyingIndex) {
    if (!gep.isGepInbounds() || varyingIndex < 1 || !AffineGep.isDirectMemoryAddress(gep)) {
      return false;
    }
    Type source = gep.getGepSourceType();
    int rank = 0;
    while (source != null && source.isArray()) {
      rank++;
      source = source.innerType;
    }
    return rank >= 2 && AffineGep.byteStride(gep, varyingIndex) > 0;
  }

  private static OffsetMatch findOffset(
      AddressFormula formula, List<AddressStream> streams) {
    if (formula == null) return null;
    for (AddressStream stream : streams) {
      Long bytes = formula.offsetFrom(stream.formula());
      if (bytes != null && bytes % Integer.BYTES == 0 && bytes >= -2048 && bytes <= 2047) {
        return new OffsetMatch(stream.recurrence(), bytes);
      }
    }
    return null;
  }

  private static boolean hasOneDirectMemoryUse(Instruction gep) {
    if (gep.getNumUses() != 1) return false;
    var use = gep.getUses().getFirst();
    Instruction user = use.getUser();
    return user.getOpcode() == Instruction.Opcode.LOAD && use.getOperandIndex() == 0
        || user.getOpcode() == Instruction.Opcode.STORE && use.getOperandIndex() == 1;
  }

  /** Whether rotation would expose an LFTR opportunity using a pointer recurrence. */
  public static boolean canOptimizeLoopExit(
      InductionVariableAnalysis.Induction induction,
      DominatorTreeAnalysis.Result dominators) {
    if (!(induction.start() instanceof Constant.Int start)
        || start.value != 0 || induction.step() != 1 || containsCall(induction.loop())) {
      return false;
    }
    Instruction branch = induction.loop().header().getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || !"slt".equals(compare.getPredicate())
        || compare.getOperand(0) != induction.phi()
        || branch.getOperand(1) != induction.latch()
        || induction.loop().contains((BasicBlock) branch.getOperand(2))) return false;

    Set<Instruction> addresses = Collections.newSetFromMap(new IdentityHashMap<>());
    boolean addressOnly = induction.phi().getUses().stream().allMatch(use -> {
      Instruction user = use.getUser();
      return user == induction.next() || user == compare
          || collectAddressUses(user, induction, dominators, addresses);
    });
    return addressOnly
        && !addresses.isEmpty()
        && addresses.size() <= MAX_ROTATED_RECURRENCES
        && addresses.stream().anyMatch(address -> {
          int index = candidateIndex(address, induction);
          return AffineGep.byteStride(address, index) == Integer.BYTES;
        });
  }

  private static boolean collectAddressUses(
      Instruction instruction,
      InductionVariableAnalysis.Induction induction,
      DominatorTreeAnalysis.Result dominators,
      Set<Instruction> addresses) {
    if (instruction.getOpcode() == Instruction.Opcode.GEP) {
      int index = candidateIndex(instruction, induction);
      if (index < 0 || !dominators.dominates(instruction.getParent(), induction.latch())) {
        return false;
      }
      addresses.add(instruction);
      return true;
    }
    if (instruction.getOpcode() != Instruction.Opcode.ADD
        && instruction.getOpcode() != Instruction.Opcode.SUB
        && instruction.getOpcode() != Instruction.Opcode.SEXT
        && instruction.getOpcode() != Instruction.Opcode.ZEXT) return false;
    return instruction.getUses().stream().allMatch(
        use -> collectAddressUses(use.getUser(), induction, dominators, addresses));
  }

  private static int candidateIndex(
      Instruction instruction, InductionVariableAnalysis.Induction induction) {
    if (instruction.getOpcode() != Instruction.Opcode.GEP
        || !AffineGep.isMemoryAddress(instruction)
        || !AffineGep.isInvariant(instruction.getOperand(0), induction.loop())) return -1;
    return AffineGep.varyingIndex(instruction, induction.phi(), induction.loop());
  }

  private static boolean containsCall(LoopAnalysis.Loop loop) {
    return loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
  }

  private static Object expressionKey(Value value, Instruction induction) {
    if (value == induction) return InductionKey.VALUE;
    if (value instanceof Constant.Int integer) {
      return List.of(integer.getType().dataType, integer.value);
    }
    if (!(value instanceof Instruction instruction)
        || instruction.getOpcode() != Instruction.Opcode.ADD
            && instruction.getOpcode() != Instruction.Opcode.SUB
            && instruction.getOpcode() != Instruction.Opcode.MUL
            && instruction.getOpcode() != Instruction.Opcode.GEP
            && instruction.getOpcode() != Instruction.Opcode.SEXT
            && instruction.getOpcode() != Instruction.Opcode.ZEXT) return value;
    List<Object> operands = new ArrayList<>();
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      operands.add(expressionKey(instruction.getOperand(index), induction));
    }
    return new ExpressionKey(
        instruction.getOpcode(),
        instruction.getType().toString(),
        instruction.getOpcode() == Instruction.Opcode.GEP
            ? instruction.getGepSourceType().toString() : "",
        List.copyOf(operands));
  }

  private enum InductionKey { VALUE }

  private record ExpressionKey(
      Instruction.Opcode opcode, String type, String detail, List<Object> operands) {}

  private record AddressStream(
      AddressFormula formula, PointerRecurrence.Result recurrence) {}

  private record OffsetMatch(PointerRecurrence.Result recurrence, long bytes) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (!LoopStrengthReduce.run(function, fam)) return PreservedAnalyses.all();
      return PreservedAnalyses.none()
          .preserve(DominatorTreeAnalysis.class)
          .preserve(LoopAnalysis.class)
          .preserve(InductionVariableAnalysis.class);
    }
  }
}
