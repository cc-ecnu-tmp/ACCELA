package accela.pass.ir.transform.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.recurrence.RankedRecurrence;
import java.util.ArrayList;
import java.util.List;

/** Builds ranked state loops and retains the original recursion as a guarded fallback. */
final class RankedRecurrenceLowering {
  private RankedRecurrenceLowering() {}

  static Function lower(accela.ir.Module module, RankedRecurrence recurrence) {
    Function original = recurrence.function();
    Function helper = new Function(uniqueName(module, original.getName() + ".rrt"), Type.INT);
    for (Function.Argument argument : original.getArguments()) {
      helper.addArgument(argument.getType(), argument.getName());
    }
    module.addFunction(helper);

    List<Integer> domainArguments = recurrence.domainArguments();
    List<Value> roots = domainArguments.stream()
        .map(index -> (Value) helper.getArguments().get(index))
        .toList();
    TabulationTable table = TabulationTable.create(module, original.getName(), roots);

    BasicBlock guard = helper.addBlock("rrt.guard");
    BasicBlock fallback = helper.addBlock("rrt.fallback");
    List<BasicBlock> sizeGuards = new ArrayList<>();
    for (int index = 0; index < roots.size(); index++) {
      sizeGuards.add(helper.addBlock("rrt.size." + index));
    }
    BasicBlock rankHeader = helper.addBlock("rrt.rank");
    List<BasicBlock> stateHeaders = new ArrayList<>();
    List<BasicBlock> stateLatches = new ArrayList<>();
    for (int index = 1; index < roots.size(); index++) {
      stateHeaders.add(helper.addBlock("rrt.state." + index));
      stateLatches.add(helper.addBlock("rrt.state." + index + ".latch"));
    }
    BasicBlock rankLatch = helper.addBlock("rrt.rank.latch");
    BasicBlock done = helper.addBlock("rrt.done");

    BasicBlock accepted =
        buildRootGuard(recurrence, roots, guard, sizeGuards, rankHeader, fallback);
    IRBuilder builder = new IRBuilder(fallback);
    Instruction fallbackCall =
        builder.createCall(original, Type.INT, helper.getArguments().toArray(Value[]::new));
    builder.createRet(fallbackCall);

    List<Value> states = new ArrayList<>();
    builder.setInsertPoint(rankHeader);
    Instruction rank = builder.createPhi(Type.INT);
    rank.addOperand(Constant.intConst(0));
    rank.addOperand(accepted);
    states.add(rank);

    for (int index = 0; index < stateHeaders.size(); index++) {
      builder.setInsertPoint(stateHeaders.get(index));
      Instruction state = builder.createPhi(Type.INT);
      state.addOperand(Constant.intConst(0));
      state.addOperand(index == 0 ? rankHeader : stateHeaders.get(index - 1));
      states.add(state);
    }

    BasicBlock cellLatch =
        stateLatches.isEmpty() ? rankLatch : stateLatches.getLast();
    BasicBlock kernel =
        RecurrenceKernelCloner.clone(
            recurrence, helper, states, table, cellLatch, fallback);
    addLoopBranches(
        roots, states, rankHeader, stateHeaders, stateLatches, rankLatch, done, kernel);

    builder.setInsertPoint(done);
    builder.createRet(builder.createLoad(Type.INT, table.address(builder, roots)));
    return helper;
  }

  private static BasicBlock buildRootGuard(
      RankedRecurrence recurrence,
      List<Value> roots,
      BasicBlock guard,
      List<BasicBlock> sizeGuards,
      BasicBlock accepted,
      BasicBlock fallback) {
    IRBuilder builder = new IRBuilder(guard);
    Value valid = rootInRange(builder, roots.getFirst());
    for (int index = 1; index < roots.size(); index++) {
      valid = builder.createAnd(valid, rootInRange(builder, roots.get(index)));
    }
    if (recurrence.rankLimit() != Integer.MAX_VALUE) {
      valid = builder.createAnd(
          valid,
          builder.createICmp(
              "sle", roots.getFirst(), Constant.intConst(recurrence.rankLimit())));
    }
    builder.createCondBr(valid, sizeGuards.getFirst(), fallback);

    Value cells = Constant.intConst(1);
    for (int index = 0; index < sizeGuards.size(); index++) {
      BasicBlock block = sizeGuards.get(index);
      BasicBlock next =
          index + 1 < sizeGuards.size() ? sizeGuards.get(index + 1) : accepted;
      builder.setInsertPoint(block);
      Value extent = builder.createAdd(roots.get(index), Constant.intConst(1));
      Value available =
          builder.createSDiv(Constant.intConst(TabulationTable.MAX_CELLS), extent);
      Value fits = builder.createICmp("sle", cells, available);
      cells = builder.createMul(cells, extent);
      builder.createCondBr(fits, next, fallback);
    }
    return sizeGuards.getLast();
  }

  private static Value rootInRange(IRBuilder builder, Value root) {
    return builder.createAnd(
        builder.createICmp("sge", root, Constant.intConst(0)),
        builder.createICmp("slt", root, Constant.intConst(TabulationTable.MAX_CELLS)));
  }

  private static void addLoopBranches(
      List<Value> roots,
      List<Value> states,
      BasicBlock rankHeader,
      List<BasicBlock> stateHeaders,
      List<BasicBlock> stateLatches,
      BasicBlock rankLatch,
      BasicBlock done,
      BasicBlock kernel) {
    IRBuilder builder = new IRBuilder(rankHeader);
    builder.createCondBr(
        builder.createICmp("sle", states.getFirst(), roots.getFirst()),
        stateHeaders.isEmpty() ? kernel : stateHeaders.getFirst(),
        done);
    for (int index = 0; index < stateHeaders.size(); index++) {
      builder.setInsertPoint(stateHeaders.get(index));
      BasicBlock body =
          index + 1 < stateHeaders.size() ? stateHeaders.get(index + 1) : kernel;
      BasicBlock exit = index == 0 ? rankLatch : stateLatches.get(index - 1);
      builder.createCondBr(
          builder.createICmp("sle", states.get(index + 1), roots.get(index + 1)),
          body,
          exit);
    }
    for (int index = 0; index < stateLatches.size(); index++) {
      builder.setInsertPoint(stateLatches.get(index));
      Value next = builder.createAdd(states.get(index + 1), Constant.intConst(1));
      builder.createBr(stateHeaders.get(index));
      Instruction phi = (Instruction) states.get(index + 1);
      phi.addOperand(next);
      phi.addOperand(stateLatches.get(index));
    }
    builder.setInsertPoint(rankLatch);
    Value nextRank = builder.createAdd(states.getFirst(), Constant.intConst(1));
    builder.createBr(rankHeader);
    Instruction rank = (Instruction) states.getFirst();
    rank.addOperand(nextRank);
    rank.addOperand(rankLatch);
  }

  private static String uniqueName(accela.ir.Module module, String base) {
    String name = base;
    for (int suffix = 1; hasFunction(module, name); suffix++) {
      name = base + "." + suffix;
    }
    return name;
  }

  private static boolean hasFunction(accela.ir.Module module, String name) {
    return module.getFunctions().stream().anyMatch(function -> function.getName().equals(name));
  }
}
