package accela.pass.ir.transform.finitestate;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.List;

/** Rewrites one legal transition loop to guarded constant-table binary lifting. */
final class FiniteStateAccelerationTransform {
  private static final String TABLE_PREFIX = "__accela_fsa_";

  private FiniteStateAccelerationTransform() {}

  static void apply(
      Function function,
      FiniteStateAccelerationMatcher.Candidate candidate,
      FiniteStateAccelerationProfitability.Plan plan) {
    accela.ir.Module module = function.getModule();
    if (module == null) {
      throw new IllegalStateException("finite-state candidate requires a module-owned function");
    }
    Type rowType = Type.array(Type.INT, candidate.domainSize());
    Type tableType = Type.array(rowType, FiniteStateAccelerationProfitability.LIFTING_LEVELS);
    GlobalVariable table = new GlobalVariable(
        uniqueGlobalName(module, function, candidate.loop().header()),
        tableType,
        tableInitializer(tableType, rowType, plan.jumpTable()),
        true);

    BasicBlock preheader = candidate.loop().preheader();
    BasicBlock header = candidate.loop().header();
    String blockPrefix = uniqueBlockPrefix(function, header.getLabel() + ".finite.state");
    BasicBlock guard = function.insertBlockAfter(preheader, blockPrefix + ".guard");
    BasicBlock prepare = function.insertBlockAfter(guard, blockPrefix + ".prepare");
    BasicBlock liftingHeader = function.insertBlockAfter(prepare, blockPrefix + ".header");
    BasicBlock testBit = function.insertBlockAfter(liftingHeader, blockPrefix + ".testbit");
    BasicBlock applyBit = function.insertBlockAfter(testBit, blockPrefix + ".apply");
    BasicBlock merge = function.insertBlockAfter(applyBit, blockPrefix + ".merge");
    BasicBlock done = function.insertBlockAfter(merge, blockPrefix + ".done");

    IRBuilder guardBuilder = new IRBuilder(guard);
    // State encoding is checked before the original loop. A negative SysY remainder therefore
    // takes the untouched loop, and zero, negative, or short iteration domains do the same.
    Value nonnegativeState = guardBuilder.createICmp(
        "sge", candidate.stateStart(), Constant.intConst(0));
    Value belowDomain = guardBuilder.createICmp(
        "slt", candidate.stateStart(), Constant.intConst(candidate.domainSize()));
    Value encodedState = guardBuilder.createAnd(nonnegativeState, belowDomain);
    Value enoughIterations = guardBuilder.createICmp(
        "sge", candidate.bound(), Constant.intConst(plan.minimumBound()));
    Value useLifting = guardBuilder.createAnd(encodedState, enoughIterations);
    guardBuilder.createCondBr(useLifting, prepare, header);

    IRBuilder prepareBuilder = new IRBuilder(prepare);
    // The true guard proves bound >= start + threshold with a nonnegative constant start, so the
    // signed subtraction is exact and lies in threshold..INT_MAX.
    Value tripCount = prepareBuilder.createSub(candidate.bound(), candidate.inductionStart());
    prepareBuilder.createBr(liftingHeader);

    IRBuilder headerBuilder = new IRBuilder(liftingHeader);
    Instruction remaining = headerBuilder.createPhi(Type.INT);
    remaining.addOperand(tripCount);
    remaining.addOperand(prepare);
    Instruction liftedState = headerBuilder.createPhi(Type.INT);
    liftedState.addOperand(candidate.stateStart());
    liftedState.addOperand(prepare);
    Instruction level = headerBuilder.createPhi(Type.INT);
    level.addOperand(Constant.intConst(0));
    level.addOperand(prepare);
    Value hasWork = headerBuilder.createICmp("ne", remaining, Constant.intConst(0));
    headerBuilder.createCondBr(hasWork, testBit, done);

    IRBuilder bitBuilder = new IRBuilder(testBit);
    Value lowBit = bitBuilder.createAnd(remaining, Constant.intConst(1));
    Value isOdd = bitBuilder.createICmp("ne", lowBit, Constant.intConst(0));
    bitBuilder.createCondBr(isOdd, applyBit, merge);

    IRBuilder applyBuilder = new IRBuilder(applyBit);
    Value levelIndex = applyBuilder.createSExt(level, Type.I64);
    Value stateIndex = applyBuilder.createSExt(liftedState, Type.I64);
    Value address = applyBuilder.createGEP(
        tableType,
        table,
        new Value[] {Constant.int64Const(0), levelIndex, stateIndex},
        true);
    Value loadedState = applyBuilder.createLoad(Type.INT, address);
    applyBuilder.createBr(merge);

    IRBuilder mergeBuilder = new IRBuilder(merge);
    Instruction nextState = mergeBuilder.createPhi(Type.INT);
    nextState.addOperand(liftedState);
    nextState.addOperand(testBit);
    nextState.addOperand(loadedState);
    nextState.addOperand(applyBit);
    Value nextRemaining = mergeBuilder.createSDiv(remaining, Constant.intConst(2));
    Value nextLevel = mergeBuilder.createAdd(level, Constant.intConst(1));
    mergeBuilder.createBr(liftingHeader);

    remaining.addOperand(nextRemaining);
    remaining.addOperand(merge);
    liftedState.addOperand(nextState);
    liftedState.addOperand(merge);
    level.addOperand(nextLevel);
    level.addOperand(merge);

    new IRBuilder(done).createBr(header);
    candidate.induction().addOperand(candidate.bound());
    candidate.induction().addOperand(done);
    candidate.state().addOperand(liftedState);
    candidate.state().addOperand(done);
    retargetPreheader(preheader, header, guard);
    retargetPhiPredecessor(candidate.induction(), preheader, guard);
    retargetPhiPredecessor(candidate.state(), preheader, guard);
    module.addGlobal(table);
  }

  private static void retargetPreheader(
      BasicBlock preheader, BasicBlock oldTarget, BasicBlock newTarget) {
    Instruction branch = preheader.getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.BR
        || branch.getNumOperands() != 1
        || branch.getOperand(0) != oldTarget) {
      throw new IllegalStateException("finite-state preheader no longer targets the loop header");
    }
    branch.setOperand(0, newTarget);
  }

  private static void retargetPhiPredecessor(
      Instruction phi, BasicBlock oldPredecessor, BasicBlock newPredecessor) {
    int matchingOperand = -1;
    for (int index = 1; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index) != oldPredecessor) continue;
      if (matchingOperand >= 0) {
        throw new IllegalStateException("finite-state phi has duplicate preheader entries");
      }
      matchingOperand = index;
    }
    if (matchingOperand < 0) {
      throw new IllegalStateException("finite-state phi has no preheader entry");
    }
    phi.setOperand(matchingOperand, newPredecessor);
  }

  private static Constant tableInitializer(Type tableType, Type rowType, int[][] values) {
    List<Constant> rows = new ArrayList<>(values.length);
    for (int[] row : values) {
      List<Constant> elements = new ArrayList<>(row.length);
      for (int element : row) elements.add(Constant.intConst(element));
      rows.add(Constant.array(rowType, elements));
    }
    return Constant.array(tableType, rows);
  }

  private static String uniqueGlobalName(
      accela.ir.Module module, Function function, BasicBlock header) {
    String base = TABLE_PREFIX + sanitize(function.getName()) + "_" + sanitize(header.getLabel());
    String name = base;
    for (int suffix = 1; hasTopLevelSymbol(module, name); suffix++) name = base + "." + suffix;
    return name;
  }

  private static boolean hasTopLevelSymbol(accela.ir.Module module, String name) {
    return module.getGlobals().stream().anyMatch(global -> global.getName().equals(name))
        || module.getFunctions().stream().anyMatch(function -> function.getName().equals(name))
        || module.getDeclares().stream().anyMatch(function -> function.getName().equals(name));
  }

  private static String uniqueBlockPrefix(Function function, String requested) {
    String prefix = requested;
    for (int suffix = 1; hasBlockWithPrefix(function, prefix); suffix++) {
      prefix = requested + "." + suffix;
    }
    return prefix;
  }

  private static boolean hasBlockWithPrefix(Function function, String prefix) {
    return function.getBlocks().stream().anyMatch(block -> block.getLabel().startsWith(prefix));
  }

  private static String sanitize(String value) {
    String sanitized = value.replaceAll("[^A-Za-z0-9_.]", "_");
    return sanitized.isEmpty() ? "anonymous" : sanitized;
  }
}
