package accela.pass.ir.transform.loop.unroll;

import static accela.pass.ir.transform.loop.unroll.LoopUnrollAndJamCandidate.incomingValue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.dependence.DependenceAnalysis;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Builds a factor-wide register tile and leaves the original scalar loop as its fallback. */
final class LoopUnrollAndJamTransform {
  private LoopUnrollAndJamTransform() {}

  static void apply(
      Function function,
      LoopUnrollAndJamCandidate candidate,
      int factor,
      DependenceAnalysis.Result dependences) {
    if (!dependences.isSafeToJam(
        candidate.outerInduction().phi(),
        candidate.innerInduction().phi(),
        candidate.outerInduction().step(),
        factor)) {
      throw new IllegalArgumentException("unroll-and-jam factor lacks a dependence proof");
    }
    Blocks blocks = createBlocks(function, candidate);
    MainHeader main = buildMainHeader(candidate, blocks, factor);
    List<Map<Value, Value>> guardedLanes =
        buildInnerGuard(candidate, blocks, main.induction(), main.lanes());
    List<Map<Value, Value>> lanes =
        buildInnerPreheader(candidate, blocks, guardedLanes);
    cloneJammedBody(candidate, blocks, lanes);
    buildMainLatch(candidate, blocks, lanes, main.induction(), factor);
    connectRemainder(candidate, blocks, main.induction());
  }

  private static Blocks createBlocks(
      Function function, LoopUnrollAndJamCandidate candidate) {
    String prefix = candidate.outerHeader().getLabel() + ".unrolljam";
    BasicBlock header =
        function.insertBlockAfter(candidate.outerPreheader(), prefix + ".header");
    BasicBlock guard = function.insertBlockAfter(header, prefix + ".guard");
    BasicBlock preheader = function.insertBlockAfter(guard, prefix + ".preheader");
    BasicBlock body = function.insertBlockAfter(preheader, prefix + ".body");
    BasicBlock latch = function.insertBlockAfter(body, prefix + ".latch");
    return new Blocks(header, guard, preheader, body, latch);
  }

  private static MainHeader buildMainHeader(
      LoopUnrollAndJamCandidate candidate, Blocks blocks, int factor) {
    Type inductionType = candidate.outerInduction().phi().getType();
    Instruction induction = Instruction.createPhi(inductionType);
    blocks.header().addInstruction(induction);
    induction.addOperand(candidate.outerInduction().start());
    induction.addOperand(candidate.outerPreheader());

    IRBuilder builder = new IRBuilder(blocks.header());
    List<Value> lanes = new ArrayList<>();
    lanes.add(induction);
    for (int lane = 1; lane < factor; lane++) {
      lanes.add(builder.createBinary(Instruction.Opcode.ADD,
          induction,
          integerConstant(inductionType,
              Math.multiplyExact(candidate.outerInduction().step(), lane))));
    }

    Value wideInduction = inductionType == Type.I64
        ? induction
        : builder.createSExt(induction, Type.I64);
    Value wideBound = candidate.outerBound().getType() == Type.I64
        ? candidate.outerBound()
        : builder.createSExt(candidate.outerBound(), Type.I64);
    long lastLaneOffset =
        Math.multiplyExact(candidate.outerInduction().step(), factor - 1L);
    Value lastLane = builder.createBinary(
        Instruction.Opcode.ADD, wideInduction, Constant.int64Const(lastLaneOffset));
    Value hasFullGroup =
        builder.createICmp(candidate.outerPredicate(), lastLane, wideBound);
    builder.createCondBr(hasFullGroup, blocks.guard(), candidate.outerHeader());
    return new MainHeader(induction, lanes);
  }

  private static List<Map<Value, Value>> buildInnerGuard(
      LoopUnrollAndJamCandidate candidate,
      Blocks blocks,
      Instruction mainInduction,
      List<Value> outerLanes) {
    IRBuilder builder = new IRBuilder(blocks.guard());
    List<Map<Value, Value>> lanes = new ArrayList<>();
    if (candidate.laneGuard() == null) {
      Map<Value, Value> shared = new IdentityHashMap<>();
      shared.put(candidate.outerInduction().phi(), mainInduction);
      mapConditionPhis(candidate, shared);
      cloneNonPhis(candidate.innerCondition(), blocks.guard(), shared, null);
      Value condition = branchConditionFor(
          candidate.innerCondition(), candidate.innerPreheader(), shared, builder);
      builder.createCondBr(condition, blocks.preheader(), blocks.latch());
      for (Value outerLane : outerLanes) {
        Map<Value, Value> values = new IdentityHashMap<>(shared);
        values.put(candidate.outerInduction().phi(), outerLane);
        lanes.add(values);
      }
      return lanes;
    }

    Value allLanesActive = Constant.boolConst(true);
    // Speculatively evaluate every side-effect-free lane guard. Loads may execute again if any
    // lane fails and control returns to the scalar loop, but SysY loads are non-volatile.
    for (Value outerLane : outerLanes) {
      Map<Value, Value> values = new IdentityHashMap<>();
      values.put(candidate.outerInduction().phi(), outerLane);
      mapBlockPhis(candidate.laneGuard(), candidate.outerHeader(), values);
      cloneNonPhis(candidate.laneGuard(), blocks.guard(), values, null);
      Value active = branchConditionFor(
          candidate.laneGuard(), candidate.innerCondition(), values, builder);
      allLanesActive = builder.createAnd(allLanesActive, active);
      lanes.add(values);
    }
    Map<Value, Value> shared = lanes.getFirst();
    mapConditionPhis(candidate, shared);
    cloneNonPhis(candidate.innerCondition(), blocks.guard(), shared, null);
    Value hasIterations = branchConditionFor(
        candidate.innerCondition(), candidate.innerPreheader(), shared, builder);
    builder.createCondBr(
        builder.createAnd(allLanesActive, hasIterations),
        blocks.preheader(),
        candidate.outerHeader());
    return lanes;
  }

  private static List<Map<Value, Value>> buildInnerPreheader(
      LoopUnrollAndJamCandidate candidate,
      Blocks blocks,
      List<Map<Value, Value>> lanes) {
    for (Map<Value, Value> values : lanes) mapConditionPhis(candidate, values);
    for (Instruction instruction : candidate.innerPreheader().getInstructions()) {
      if (instruction.getOpcode() == Instruction.Opcode.PHI || instruction.isTerminator()) {
        continue;
      }
      if (LoopUnrollAndJamCandidate.dependsOn(
          instruction, candidate.outerInduction().phi())) {
        for (Map<Value, Value> lane : lanes) {
          cloneInstruction(instruction, blocks.preheader(), lane);
        }
      } else {
        Instruction clone = cloneInstruction(
            instruction, blocks.preheader(), lanes.getFirst());
        for (Map<Value, Value> lane : lanes) lane.put(instruction, clone);
      }
    }
    new IRBuilder(blocks.preheader()).createBr(blocks.body());
    return lanes;
  }

  private static void cloneJammedBody(
      LoopUnrollAndJamCandidate candidate,
      Blocks blocks,
      List<Map<Value, Value>> lanes) {
    Instruction originalInduction = candidate.innerInduction().phi();
    Instruction induction = Instruction.createPhi(originalInduction.getType());
    blocks.body().addInstruction(induction);
    induction.addOperand(remap(
        incomingValue(originalInduction, candidate.innerPreheader()),
        lanes.getFirst()));
    induction.addOperand(blocks.preheader());

    List<Instruction> recurrences = phis(candidate.innerBody()).stream()
        .filter(phi -> phi != originalInduction)
        .toList();
    List<List<Instruction>> laneRecurrences = new ArrayList<>();
    for (Map<Value, Value> lane : lanes) {
      lane.put(originalInduction, induction);
      List<Instruction> clonedPhis = new ArrayList<>();
      for (Instruction recurrence : recurrences) {
        Instruction clone = Instruction.createPhi(recurrence.getType());
        blocks.body().addInstruction(clone);
        clone.addOperand(remap(
            incomingValue(recurrence, candidate.innerPreheader()), lane));
        clone.addOperand(blocks.preheader());
        lane.put(recurrence, clone);
        clonedPhis.add(clone);
      }
      laneRecurrences.add(clonedPhis);
    }

    List<Value> laneDependencies = new ArrayList<>(recurrences);
    laneDependencies.add(candidate.outerInduction().phi());
    for (Instruction instruction : candidate.innerBody().getInstructions()) {
      if (instruction.getOpcode() == Instruction.Opcode.PHI || instruction.isTerminator()) {
        continue;
      }
      if (dependsOnAny(instruction, laneDependencies)) {
        for (Map<Value, Value> lane : lanes) {
          cloneInstruction(instruction, blocks.body(), lane);
        }
      } else {
        // Memory operations are shared only after the entry-point dependence proof has
        // established that adjacent outer lanes cannot observe conflicting accesses.
        Instruction clone = cloneInstruction(instruction, blocks.body(), lanes.getFirst());
        for (Map<Value, Value> lane : lanes) lane.put(instruction, clone);
      }
    }

    induction.addOperand(remap(candidate.innerInduction().next(), lanes.getFirst()));
    induction.addOperand(blocks.body());
    for (int laneIndex = 0; laneIndex < lanes.size(); laneIndex++) {
      for (int recurrenceIndex = 0; recurrenceIndex < recurrences.size(); recurrenceIndex++) {
        Instruction original = recurrences.get(recurrenceIndex);
        Instruction clone = laneRecurrences.get(laneIndex).get(recurrenceIndex);
        clone.addOperand(remap(
            incomingValue(original, candidate.innerBody()),
            lanes.get(laneIndex)));
        clone.addOperand(blocks.body());
      }
    }

    Value condition = remap(
        candidate.innerBody().getTerminator().getOperand(0), lanes.getFirst());
    IRBuilder builder = new IRBuilder(blocks.body());
    if (candidate.innerBody().getTerminator().getOperand(1)
        == candidate.innerBody()) {
      builder.createCondBr(condition, blocks.body(), blocks.latch());
    } else {
      builder.createCondBr(condition, blocks.latch(), blocks.body());
    }
  }

  private static void buildMainLatch(
      LoopUnrollAndJamCandidate candidate,
      Blocks blocks,
      List<Map<Value, Value>> lanes,
      Instruction mainInduction,
      int factor) {
    List<Instruction> resultPhis = phis(candidate.innerExit());
    for (Map<Value, Value> lane : lanes) {
      for (Instruction result : resultPhis) {
        Instruction clone = Instruction.createPhi(result.getType());
        blocks.latch().addInstruction(clone);
        clone.addOperand(remap(
            incomingValue(result, candidate.innerCondition()), lane));
        clone.addOperand(blocks.guard());
        clone.addOperand(remap(
            incomingValue(result, candidate.innerBody()), lane));
        clone.addOperand(blocks.body());
        lane.put(result, clone);
      }
    }

    for (Map<Value, Value> lane : lanes) {
      cloneNonPhis(
          candidate.innerExit(), blocks.latch(), lane, candidate.outerInduction().next());
    }
    IRBuilder builder = new IRBuilder(blocks.latch());
    Value next = builder.createBinary(Instruction.Opcode.ADD,
        mainInduction,
        integerConstant(mainInduction.getType(),
            Math.multiplyExact(candidate.outerInduction().step(), factor)));
    builder.createBr(blocks.header());
    mainInduction.addOperand(next);
    mainInduction.addOperand(blocks.latch());
  }

  private static void connectRemainder(
      LoopUnrollAndJamCandidate candidate, Blocks blocks, Instruction mainInduction) {
    retarget(
        candidate.outerPreheader().getTerminator(),
        candidate.outerHeader(),
        blocks.header());
    Instruction scalarInduction = candidate.outerInduction().phi();
    for (int index = 0; index < scalarInduction.getNumOperands(); index += 2) {
      if (scalarInduction.getOperand(index + 1) != candidate.outerPreheader()) continue;
      scalarInduction.setOperand(index, mainInduction);
      scalarInduction.setOperand(index + 1, blocks.header());
      if (candidate.laneGuard() != null) {
        scalarInduction.addOperand(mainInduction);
        scalarInduction.addOperand(blocks.guard());
      }
      return;
    }
    throw new IllegalStateException("outer induction has no preheader incoming edge");
  }

  private static void cloneNonPhis(
      BasicBlock source,
      BasicBlock destination,
      Map<Value, Value> values,
      Instruction skipped) {
    for (Instruction instruction : source.getInstructions()) {
      if (instruction.getOpcode() == Instruction.Opcode.PHI
          || instruction.isTerminator()
          || instruction == skipped) continue;
      cloneInstruction(instruction, destination, values);
    }
  }

  private static Instruction cloneInstruction(
      Instruction instruction, BasicBlock destination, Map<Value, Value> values) {
    Instruction clone = instruction.copyWithoutOperands();
    clone.setName(null);
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      clone.addOperand(remap(instruction.getOperand(index), values));
    }
    destination.addInstruction(clone);
    values.put(instruction, clone);
    return clone;
  }

  private static boolean dependsOnAny(Value value, List<Value> dependencies) {
    for (Value dependency : dependencies) {
      if (LoopUnrollAndJamCandidate.dependsOn(value, dependency)) return true;
    }
    return false;
  }

  private static Value branchConditionFor(
      BasicBlock source,
      BasicBlock success,
      Map<Value, Value> values,
      IRBuilder builder) {
    Instruction branch = source.getTerminator();
    Value condition = remap(branch.getOperand(0), values);
    return branch.getOperand(1) == success
        ? condition
        : builder.createXor(condition, Constant.boolConst(true));
  }

  private static List<Instruction> phis(BasicBlock block) {
    return block.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
  }

  private static void mapConditionPhis(
      LoopUnrollAndJamCandidate candidate, Map<Value, Value> values) {
    BasicBlock predecessor = candidate.laneGuard() == null
        ? candidate.outerHeader()
        : candidate.laneGuard();
    mapBlockPhis(candidate.innerCondition(), predecessor, values);
  }

  private static void mapBlockPhis(
      BasicBlock block, BasicBlock predecessor, Map<Value, Value> values) {
    for (Instruction phi : phis(block)) {
      Value incoming = incomingValue(phi, predecessor);
      if (incoming == null) {
        throw new IllegalStateException("guard PHI has no incoming value from its predecessor");
      }
      values.put(phi, remap(incoming, values));
    }
  }

  private static Value remap(Value value, Map<Value, Value> values) {
    return values.getOrDefault(value, value);
  }

  private static Constant.Int integerConstant(Type type, long value) {
    return type == Type.I64 ? Constant.int64Const(value) : Constant.intConst(value);
  }

  private static void retarget(
      Instruction branch, BasicBlock oldTarget, BasicBlock newTarget) {
    boolean replaced = false;
    for (int index = 0; index < branch.getNumOperands(); index++) {
      if (branch.getOperand(index) == oldTarget) {
        branch.setOperand(index, newTarget);
        replaced = true;
      }
    }
    if (!replaced) throw new IllegalStateException("preheader does not branch to the outer loop");
  }

  private record Blocks(
        BasicBlock header,
        BasicBlock guard,
        BasicBlock preheader,
        BasicBlock body,
        BasicBlock latch) {}

  private record MainHeader(Instruction induction, List<Value> lanes) {}
}
