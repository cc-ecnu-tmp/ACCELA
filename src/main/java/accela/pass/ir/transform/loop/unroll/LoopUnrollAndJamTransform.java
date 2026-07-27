package accela.pass.ir.transform.loop.unroll;

import static accela.pass.ir.transform.loop.unroll.LoopUnrollAndJamCandidate.incomingValue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Builds a factor-wide jammed main loop and leaves the original scalar loop as its remainder. */
final class LoopUnrollAndJamTransform {
  private LoopUnrollAndJamTransform() {}

  static void apply(
      Function function, LoopUnrollAndJamCandidate candidate, int factor) {
    Blocks blocks = createBlocks(function, candidate);
    MainHeader main = buildMainHeader(candidate, blocks, factor);
    Map<Value, Value> shared = buildInnerGuard(candidate, blocks, main.induction());
    List<Map<Value, Value>> lanes =
        buildInnerPreheader(candidate, blocks, shared, main.lanes());
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
    Instruction induction = Instruction.createPhi(Type.INT);
    blocks.header().addInstruction(induction);
    induction.addOperand(candidate.outerInduction().start());
    induction.addOperand(candidate.outerPreheader());

    IRBuilder builder = new IRBuilder(blocks.header());
    List<Value> lanes = new ArrayList<>();
    lanes.add(induction);
    for (int lane = 1; lane < factor; lane++) {
      lanes.add(builder.createAdd(
          induction,
          Constant.intConst(Math.multiplyExact(candidate.outerInduction().step(), lane))));
    }

    Value wideInduction = builder.createSExt(induction, Type.I64);
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

  private static Map<Value, Value> buildInnerGuard(
      LoopUnrollAndJamCandidate candidate, Blocks blocks, Instruction mainInduction) {
    Map<Value, Value> values = new IdentityHashMap<>();
    values.put(candidate.outerInduction().phi(), mainInduction);
    mapConditionPhis(candidate, values);
    cloneNonPhis(candidate.innerCondition(), blocks.guard(), values, null);

    Value condition = remap(
        candidate.innerCondition().getTerminator().getOperand(0), values);
    IRBuilder builder = new IRBuilder(blocks.guard());
    if (candidate.innerCondition().getTerminator().getOperand(1)
        == candidate.innerPreheader()) {
      builder.createCondBr(condition, blocks.preheader(), blocks.latch());
    } else {
      builder.createCondBr(condition, blocks.latch(), blocks.preheader());
    }
    return values;
  }

  private static List<Map<Value, Value>> buildInnerPreheader(
      LoopUnrollAndJamCandidate candidate,
      Blocks blocks,
      Map<Value, Value> shared,
      List<Value> outerLanes) {
    List<Map<Value, Value>> lanes = new ArrayList<>();
    for (Value outerLane : outerLanes) {
      Map<Value, Value> values = new IdentityHashMap<>(shared);
      values.put(candidate.outerInduction().phi(), outerLane);
      mapConditionPhis(candidate, values);
      cloneNonPhis(candidate.innerPreheader(), blocks.preheader(), values, null);
      lanes.add(values);
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

    for (Map<Value, Value> lane : lanes) {
      cloneNonPhis(candidate.innerBody(), blocks.body(), lane, null);
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
    Value next = builder.createAdd(
        mainInduction,
        Constant.intConst(Math.multiplyExact(candidate.outerInduction().step(), factor)));
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
      Instruction clone = instruction.copyWithoutOperands();
      clone.setName(null);
      for (int index = 0; index < instruction.getNumOperands(); index++) {
        clone.addOperand(remap(instruction.getOperand(index), values));
      }
      destination.addInstruction(clone);
      values.put(instruction, clone);
    }
  }

  private static List<Instruction> phis(BasicBlock block) {
    return block.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
  }

  private static void mapConditionPhis(
      LoopUnrollAndJamCandidate candidate, Map<Value, Value> values) {
    for (Instruction phi : phis(candidate.innerCondition())) {
      values.put(phi, remap(incomingValue(phi, candidate.outerHeader()), values));
    }
  }

  private static Value remap(Value value, Map<Value, Value> values) {
    return values.getOrDefault(value, value);
  }

  private static void retarget(
      Instruction branch, BasicBlock oldTarget, BasicBlock newTarget) {
    for (int index = 0; index < branch.getNumOperands(); index++) {
      if (branch.getOperand(index) == oldTarget) {
        branch.setOperand(index, newTarget);
        return;
      }
    }
    throw new IllegalStateException("preheader does not branch to the outer loop");
  }

  private record Blocks(
        BasicBlock header,
        BasicBlock guard,
        BasicBlock preheader,
        BasicBlock body,
        BasicBlock latch) {}

  private record MainHeader(Instruction induction, List<Value> lanes) {}
}
