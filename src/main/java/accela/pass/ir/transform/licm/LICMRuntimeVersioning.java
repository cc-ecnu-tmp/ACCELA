package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAccessAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Versions a small canonical loop when pointer inequality makes an invariant load hoistable.
 *
 * <p>The first implementation deliberately emits only exact-address checks. SCEV range facts in
 * {@link LoopAccessAnalysis} remain available to later work, but are not converted into guards.
 */
public final class LICMRuntimeVersioning {
  private static final int MIN_TRIP_COUNT = 8;
  private static final int MAX_TRIP_COUNT = 4096;
  private static final int MAX_LOOP_BLOCKS = 4;
  private static final int MAX_CLONED_INSTRUCTIONS = 20;
  private static final int MAX_RUNTIME_CHECKS = 2;
  private static int nextId;

  private LICMRuntimeVersioning() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops =
        fam.getResult(LoopAnalysis.class, function).loops();
    LoopAccessAnalysis.Result accesses =
        fam.getResult(LoopAccessAnalysis.class, function);
    ScalarEvolutionAnalysis.Result scev =
        fam.getResult(ScalarEvolutionAnalysis.class, function);
    GlobalModRefAnalysis.Result modRef =
        function.getModule() == null ? null : GlobalModRefAnalysis.analyze(function.getModule());
    for (LoopAnalysis.Loop loop : loops) {
      if (!isInnermost(loop, loops) || !isCanonicalAndProfitable(loop, scev)) continue;
      Candidate candidate = findCandidate(accesses.getInfo(loop), modRef);
      if (candidate == null || !hasCanonicalLiveOuts(loop)) continue;
      transform(function, candidate);
      fam.invalidate(function, PreservedAnalyses.none());
      // The guarded load is now in the fast preheader. Ordinary LICM completes hoisting of its
      // dependent invariant computations while retaining the untouched fallback loop.
      LICM.runOnFunction(function, fam);
      return true;
    }
    return false;
  }

  private static Candidate findCandidate(
      LoopAccessAnalysis.LoopAccessInfo info,
      GlobalModRefAnalysis.Result modRef) {
    if (info == null) return null;
    for (LoopAccessAnalysis.Access load : info.loads()) {
      if (!load.loopInvariant()
          || load.instruction().getParent() != info.loop().header()
          || !isScalar(load.location().accessType())
          || isDefinedInLoop(load.location().pointer(), info.loop())
          || info.callMayWrite(load, modRef)) {
        continue;
      }
      Set<Value> checkedPointers = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
      List<LoopAccessAnalysis.Access> conflicts = new ArrayList<>();
      boolean unguardable = false;
      for (LoopAccessAnalysis.Access store : info.stores()) {
        if (!PointerProvenance.mayAlias(
            load.location().pointer(), store.location().pointer())) {
          continue;
        }
        if (load.location().pointer() == store.location().pointer()
            || !store.loopInvariant()
            || isDefinedInLoop(store.location().pointer(), info.loop())
            || !load.location().hasSameAccessShape(store.location())
            || !isScalar(store.location().accessType())) {
          unguardable = true;
          break;
        }
        conflicts.add(store);
        checkedPointers.add(store.location().pointer());
      }
      if (!unguardable
          && !conflicts.isEmpty()
          && checkedPointers.size() <= MAX_RUNTIME_CHECKS) {
        return new Candidate(info.loop(), load, List.copyOf(conflicts));
      }
    }
    return null;
  }

  private static boolean isCanonicalAndProfitable(
      LoopAnalysis.Loop loop, ScalarEvolutionAnalysis.Result scev) {
    BasicBlock preheader = loop.preheader();
    if (preheader == null
        || loop.latches().size() != 1
        || loop.blocks().size() > MAX_LOOP_BLOCKS
        || preheader.getTerminator() == null
        || preheader.getTerminator().getOpcode() != Instruction.Opcode.BR
        || preheader.getTerminator().getOperand(0) != loop.header()) {
      return false;
    }
    int instructionCount = loop.blocks().stream()
        .mapToInt(block -> block.getInstructions().size())
        .sum();
    if (instructionCount > MAX_CLONED_INSTRUCTIONS) return false;
    BigInteger tripCount = scev.getConstantBackedgeTakenCount(loop).orElse(null);
    if (tripCount == null) return false;
    tripCount = tripCount.add(BigInteger.ONE);
    return tripCount.compareTo(BigInteger.valueOf(MIN_TRIP_COUNT)) >= 0
        && tripCount.compareTo(BigInteger.valueOf(MAX_TRIP_COUNT)) <= 0;
  }

  private static boolean hasCanonicalLiveOuts(LoopAnalysis.Loop loop) {
    Set<BasicBlock> exits = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)) exits.add(successor);
      }
    }
    if (exits.size() != 1) return false;
    BasicBlock exit = exits.iterator().next();
    if (exit.getPredecessors().stream().anyMatch(predecessor -> !loop.contains(predecessor))) {
      return false;
    }
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        for (Use use : instruction.getUses()) {
          if (loop.contains(use.getUser().getParent())) continue;
          if (!isExitPhiUse(use, loop, exit)) return false;
        }
      }
    }
    return true;
  }

  private static boolean isExitPhiUse(
      Use use, LoopAnalysis.Loop loop, BasicBlock exit) {
    Instruction user = use.getUser();
    int index = use.getOperandIndex();
    return user.getParent() == exit
        && user.getOpcode() == Instruction.Opcode.PHI
        && (index & 1) == 0
        && index + 1 < user.getNumOperands()
        && user.getOperand(index + 1) instanceof BasicBlock predecessor
        && loop.contains(predecessor);
  }

  private static void transform(Function function, Candidate candidate) {
    int id = nextId++;
    LoopAnalysis.Loop loop = candidate.loop();
    BasicBlock oldPreheader = loop.preheader();
    BasicBlock fastPreheader =
        function.insertBlockAfter(oldPreheader, loop.header().getLabel() + ".licm.fast.preheader." + id);
    List<BasicBlock> sources =
        function.getBlocks().stream().filter(loop::contains).toList();
    Map<BasicBlock, BasicBlock> blocks = new IdentityHashMap<>();
    blocks.put(oldPreheader, fastPreheader);
    BasicBlock insertion = sources.getLast();
    for (BasicBlock source : sources) {
      BasicBlock copy =
          function.insertBlockAfter(insertion, source.getLabel() + ".licm.fast." + id);
      blocks.put(source, copy);
      insertion = copy;
    }

    Map<Value, Value> values = new IdentityHashMap<>();
    Map<Instruction, Instruction> instructions = new IdentityHashMap<>();
    for (BasicBlock sourceBlock : sources) {
      for (Instruction source : sourceBlock.getInstructions()) {
        Instruction copy = source.copyWithoutOperands();
        copy.setName(source.getName() == null ? null : source.getName() + ".licm.fast");
        blocks.get(sourceBlock).addInstruction(copy);
        values.put(source, copy);
        instructions.put(source, copy);
      }
    }
    for (var entry : instructions.entrySet()) {
      Instruction source = entry.getKey();
      Instruction copy = entry.getValue();
      for (int index = 0; index < source.getNumOperands(); index++) {
        Value operand = source.getOperand(index);
        copy.addOperand(
            operand instanceof BasicBlock block
                ? blocks.getOrDefault(block, block)
                : values.getOrDefault(operand, operand));
      }
    }
    extendExitPhis(loop, blocks, values);

    Instruction clonedLoad = instructions.get(candidate.load().instruction());
    clonedLoad.getParent().remove(clonedLoad);
    fastPreheader.addInstruction(clonedLoad);
    new IRBuilder(fastPreheader).createBr(blocks.get(loop.header()));

    Instruction oldTerminator = oldPreheader.getTerminator();
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(oldTerminator);
    Value guard = null;
    Set<Value> checked = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    for (LoopAccessAnalysis.Access conflict : candidate.conflicts()) {
      Value pointer = conflict.location().pointer();
      if (!checked.add(pointer)) continue;
      Value check =
          builder.createICmp("ne", candidate.load().location().pointer(), pointer);
      guard = guard == null ? check : builder.createAnd(guard, check);
    }
    builder.createCondBr(guard, fastPreheader, loop.header());
    oldTerminator.eraseFromParent();
  }

  private static void extendExitPhis(
      LoopAnalysis.Loop loop,
      Map<BasicBlock, BasicBlock> blocks,
      Map<Value, Value> values) {
    Set<BasicBlock> exits = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)) exits.add(successor);
      }
    }
    for (BasicBlock exit : exits) {
      for (Instruction phi : exit.getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        List<Value> additions = new ArrayList<>();
        for (int index = 0; index < phi.getNumOperands(); index += 2) {
          BasicBlock predecessor = (BasicBlock) phi.getOperand(index + 1);
          if (!loop.contains(predecessor)) continue;
          additions.add(values.getOrDefault(phi.getOperand(index), phi.getOperand(index)));
          additions.add(blocks.get(predecessor));
        }
        additions.forEach(phi::addOperand);
      }
    }
  }

  private static boolean isDefinedInLoop(Value value, LoopAnalysis.Loop loop) {
    return value instanceof Instruction instruction
        && instruction.getParent() != null
        && loop.contains(instruction.getParent());
  }

  private static boolean isScalar(Type type) {
    return !type.isArray() && !type.isVector() && type != Type.VOID;
  }

  private static boolean isInnermost(
      LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> loops) {
    return loops.stream().noneMatch(
        other ->
            other != loop
                && loop.contains(other.header())
                && other.blocks().size() < loop.blocks().size());
  }

  private record Candidate(
      LoopAnalysis.Loop loop,
      LoopAccessAnalysis.Access load,
      List<LoopAccessAnalysis.Access> conflicts) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LICMRuntimeVersioning.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
