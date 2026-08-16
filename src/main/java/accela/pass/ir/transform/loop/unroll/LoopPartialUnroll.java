package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/**
 * Forms a guarded factor-two/factor-four chunk loop and leaves the original loop as its scalar
 * remainder.  The guard tests the induction value of the last iteration in a chunk, so no cloned
 * iteration executes unless every iteration in the chunk is legal.
 */
final class LoopPartialUnroll {
  private static final int MAX_BODY_INSTRUCTIONS = 24;
  private static final int MAX_EXPANDED_INSTRUCTIONS = 96;
  private static final int MAX_ESTIMATED_LIVE_VALUES = 48;
  private static final int MAX_EXPANDED_MEMORY_OPERATIONS = 4;
  private static int nextId;

  private LoopPartialUnroll() {}

  static boolean run(Function function, FunctionAnalysisManager fam) {
    Candidate candidate = find(function, fam);
    if (candidate == null) return false;
    transform(function, candidate);
    return true;
  }

  private static Candidate find(Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops =
        fam.getResult(LoopAnalysis.class, function).loops();
    ScalarEvolutionAnalysis.Result scev =
        fam.getResult(ScalarEvolutionAnalysis.class, function);
    for (InductionVariableAnalysis.Induction induction :
        fam.getResult(InductionVariableAnalysis.class, function).inductions()) {
      LoopAnalysis.Loop loop = induction.loop();
      if (hasSubloop(loop, loops)
          || loop.header().getLabel().contains(".unroll.")
          || induction.predecessor().getLabel().contains(".unroll.")) continue;
      Exit exit = matchExit(loop);
      if (exit == null || !compareUsesInductionOnly(exit.compare(), induction.phi(), loop)) continue;
      if (!isMonotoneChunkPredicate(exit.compare(), induction.phi(), induction.step(), exit.body())) {
        continue;
      }
      BigInteger count = scev.getConstantBackedgeTakenCount(loop).orElse(null);
      if (count != null && count.signum() <= 0) continue;
      // Small constants are more profitable on the existing full-unroll path.
      if (count != null && count.compareTo(BigInteger.valueOf(8)) <= 0) continue;
      // A dynamic memory loop pays an extra guard and scalar-remainder entry on common short
      // paths. Without a trip-count/profile proof this regresses guarded UAJ fallback code.
      if (count == null && memoryOperations(loop) != 0) continue;

      int instructions = instructionCount(loop);
      int phis = headerPhis(loop.header()).size();
      int factor = instructions <= 12 && phis <= 4 ? 4 : 2;
      if (containsStore(loop)
          || instructions > MAX_BODY_INSTRUCTIONS
          || instructions * factor > MAX_EXPANDED_INSTRUCTIONS
          || estimatedValues(loop) * factor > MAX_ESTIMATED_LIVE_VALUES
          || memoryOperations(loop) * factor > MAX_EXPANDED_MEMORY_OPERATIONS) continue;
      LoopUnrollCandidate adapter = new LoopUnrollCandidate(
          loop, induction, exit.compare(), exit.body(), exit.exit(), factor);
      if (isStructurallySafe(function, adapter)) {
        return new Candidate(adapter, factor);
      }
    }
    return null;
  }

  private static void transform(Function function, Candidate plan) {
    LoopUnrollCandidate candidate = plan.loop();
    int id = nextId++;
    List<BasicBlock> sources = function.getBlocks().stream()
        .filter(candidate.loop()::contains)
        .toList();
    BasicBlock mainHeader = function.insertBlockAfter(
        candidate.induction().predecessor(), candidate.loop().header().getLabel()
            + ".unroll.main." + id);
    List<Map<BasicBlock, BasicBlock>> iterations =
        createIterationBlocks(function, sources, plan.factor(), id);

    Map<Instruction, Value> initial = new IdentityHashMap<>();
    Map<Instruction, Instruction> mainPhis = new IdentityHashMap<>();
    for (Instruction phi : headerPhis(candidate.loop().header())) {
      Instruction mainPhi = Instruction.createPhi(phi.getType());
      if (phi.getName() != null) mainPhi.setName(phi.getName() + ".unroll");
      mainHeader.addInstructionToFront(mainPhi);
      Value start = incomingValue(phi, candidate.induction().predecessor());
      mainPhi.addOperand(start);
      mainPhi.addOperand(candidate.induction().predecessor());
      mainPhis.put(phi, mainPhi);
      initial.put(phi, mainPhi);
    }

    Map<Instruction, Value> carried = initial;
    Map<Value, Value> lastValues = Map.of();
    for (int iteration = 0; iteration < plan.factor(); iteration++) {
      lastValues = LoopIterationCloner.clone(
          candidate, sources, iterations, iteration, carried, mainHeader);
      carried = carriedValues(candidate, lastValues);
    }
    BasicBlock clonedLatch =
        iterations.getLast().get(candidate.induction().latch());
    for (var entry : mainPhis.entrySet()) {
      Value backedge = incomingValue(entry.getKey(), candidate.induction().latch());
      entry.getValue().addOperand(lastValues.getOrDefault(backedge, backedge));
      entry.getValue().addOperand(clonedLatch);
    }

    IRBuilder guardBuilder = new IRBuilder(mainHeader);
    Value prospective = mainPhis.get(candidate.induction().phi());
    for (int i = 1; i < plan.factor(); i++) {
      prospective = guardBuilder.createAdd(
          prospective, Constant.intConst((int) candidate.induction().step()));
    }
    Instruction guard = candidate.compare().copyWithoutOperands();
    mainHeader.addInstruction(guard);
    for (int index = 0; index < candidate.compare().getNumOperands(); index++) {
      Value operand = candidate.compare().getOperand(index);
      guard.addOperand(operand == candidate.induction().phi() ? prospective : operand);
    }
    BasicBlock first = iterations.getFirst().get(candidate.loop().header());
    boolean bodyOnTrue = ((BasicBlock) candidate.loop().header().getTerminator().getOperand(1))
        == candidate.body();
    guardBuilder.createCondBr(
        guard,
        bodyOnTrue ? first : candidate.loop().header(),
        bodyOnTrue ? candidate.loop().header() : first);

    retarget(candidate.induction().predecessor(), candidate.loop().header(), mainHeader);
    for (var entry : mainPhis.entrySet()) {
      replaceIncoming(
          entry.getKey(),
          candidate.induction().predecessor(),
          entry.getValue(),
          mainHeader);
    }
  }

  private static List<Map<BasicBlock, BasicBlock>> createIterationBlocks(
      Function function, List<BasicBlock> sources, int factor, int id) {
    BasicBlock insertion = sources.getLast();
    List<Map<BasicBlock, BasicBlock>> result = new ArrayList<>();
    for (int iteration = 0; iteration < factor; iteration++) {
      Map<BasicBlock, BasicBlock> blocks = new IdentityHashMap<>();
      for (BasicBlock source : sources) {
        BasicBlock copy = function.insertBlockAfter(
            insertion, source.getLabel() + ".unroll.chunk." + id + "." + iteration);
        blocks.put(source, copy);
        insertion = copy;
      }
      result.add(blocks);
    }
    return result;
  }

  private static Map<Instruction, Value> carriedValues(
      LoopUnrollCandidate candidate, Map<Value, Value> values) {
    Map<Instruction, Value> result = new IdentityHashMap<>();
    for (Instruction phi : headerPhis(candidate.loop().header())) {
      Value backedge = incomingValue(phi, candidate.induction().latch());
      result.put(phi, values.getOrDefault(backedge, backedge));
    }
    return result;
  }

  private static boolean isStructurallySafe(
      Function function, LoopUnrollCandidate candidate) {
    if (candidate.loop().latches().size() != 1
        || candidate.loop().header() == candidate.induction().latch()
        || candidate.induction().predecessor().getSuccessors().size() != 1
        || !function.getBlocks().containsAll(candidate.loop().blocks())) return false;
    Instruction latchTerminator = candidate.induction().latch().getTerminator();
    if (latchTerminator == null
        || latchTerminator.getOpcode() != Instruction.Opcode.BR
        || latchTerminator.getOperand(0) != candidate.loop().header()) return false;
    int exits = 0;
    for (BasicBlock block : candidate.loop().blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.CALL
            || instruction.getOpcode() == Instruction.Opcode.ALLOCA
            || instruction.getOpcode() == Instruction.Opcode.RET
            || instruction.getOpcode() == Instruction.Opcode.PHI
                && block != candidate.loop().header()) return false;
      }
      for (BasicBlock successor : block.getSuccessors()) {
        if (candidate.loop().contains(successor)) continue;
        if (block != candidate.loop().header() || successor != candidate.exit()) return false;
        exits++;
      }
    }
    for (Instruction phi : headerPhis(candidate.loop().header())) {
      if (phi.getNumOperands() != 4
          || incomingValue(phi, candidate.induction().predecessor()) == null
          || incomingValue(phi, candidate.induction().latch()) == null) return false;
    }
    return exits == 1;
  }

  private static boolean compareUsesInductionOnly(
      Instruction compare, Instruction induction, LoopAnalysis.Loop loop) {
    boolean found = false;
    for (int i = 0; i < compare.getNumOperands(); i++) {
      Value operand = compare.getOperand(i);
      if (operand == induction) found = true;
      else if (operand instanceof Instruction instruction
          && loop.contains(instruction.getParent())) return false;
    }
    return found;
  }

  private static boolean isMonotoneChunkPredicate(
      Instruction compare, Instruction induction, long step, BasicBlock body) {
    String predicate = compare.getPredicate();
    if (compare.getOperand(1) == induction) {
      predicate = switch (predicate) {
        case "slt" -> "sgt";
        case "sle" -> "sge";
        case "sgt" -> "slt";
        case "sge" -> "sle";
        case "ult" -> "ugt";
        case "ule" -> "uge";
        case "ugt" -> "ult";
        case "uge" -> "ule";
        default -> predicate;
      };
    }
    boolean bodyOnTrue =
        ((BasicBlock) compare.getParent().getTerminator().getOperand(1)) == body;
    if (!bodyOnTrue) {
      predicate = switch (predicate) {
        case "slt" -> "sge";
        case "sle" -> "sgt";
        case "sgt" -> "sle";
        case "sge" -> "slt";
        case "ult" -> "uge";
        case "ule" -> "ugt";
        case "ugt" -> "ule";
        case "uge" -> "ult";
        default -> predicate;
      };
    }
    return step > 0
        ? predicate.equals("slt") || predicate.equals("sle")
            || predicate.equals("ult") || predicate.equals("ule")
        : predicate.equals("sgt") || predicate.equals("sge")
            || predicate.equals("ugt") || predicate.equals("uge");
  }

  private static Exit matchExit(LoopAnalysis.Loop loop) {
    Instruction branch = loop.header().getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) return null;
    BasicBlock onTrue = (BasicBlock) branch.getOperand(1);
    BasicBlock onFalse = (BasicBlock) branch.getOperand(2);
    if (loop.contains(onTrue) == loop.contains(onFalse)) return null;
    return loop.contains(onTrue)
        ? new Exit(compare, onTrue, onFalse)
        : new Exit(compare, onFalse, onTrue);
  }

  private static List<Instruction> headerPhis(BasicBlock header) {
    List<Instruction> phis = new ArrayList<>();
    for (Instruction instruction : header.getInstructions()) {
      if (instruction.getOpcode() != Instruction.Opcode.PHI) break;
      phis.add(instruction);
    }
    return phis;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int i = 0; i < phi.getNumOperands(); i += 2) {
      if (phi.getOperand(i + 1) == predecessor) return phi.getOperand(i);
    }
    return null;
  }

  private static void replaceIncoming(
      Instruction phi, BasicBlock oldPredecessor, Value value, BasicBlock predecessor) {
    for (int i = 0; i < phi.getNumOperands(); i += 2) {
      if (phi.getOperand(i + 1) == oldPredecessor) {
        phi.setOperand(i, value);
        phi.setOperand(i + 1, predecessor);
        return;
      }
    }
    throw new IllegalStateException("missing PHI entry edge");
  }

  private static void retarget(BasicBlock block, BasicBlock oldTarget, BasicBlock target) {
    Instruction terminator = block.getTerminator();
    for (int i = 0; i < terminator.getNumOperands(); i++) {
      if (terminator.getOperand(i) == oldTarget) terminator.setOperand(i, target);
    }
  }

  private static boolean hasSubloop(
      LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> loops) {
    return loops.stream().anyMatch(other -> other != loop
        && loop.contains(other.header()) && other.blocks().size() < loop.blocks().size());
  }

  private static int instructionCount(LoopAnalysis.Loop loop) {
    return loop.blocks().stream().mapToInt(block -> block.getInstructions().size()).sum();
  }

  private static int estimatedValues(LoopAnalysis.Loop loop) {
    return (int) loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(Instruction::hasResult)
        .count();
  }

  private static int memoryOperations(LoopAnalysis.Loop loop) {
    return (int) loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.LOAD
            || instruction.getOpcode() == Instruction.Opcode.STORE)
        .count();
  }

  private static boolean containsStore(LoopAnalysis.Loop loop) {
    return loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.STORE);
  }

  private record Exit(Instruction compare, BasicBlock body, BasicBlock exit) {}

  private record Candidate(LoopUnrollCandidate loop, int factor) {}
}
