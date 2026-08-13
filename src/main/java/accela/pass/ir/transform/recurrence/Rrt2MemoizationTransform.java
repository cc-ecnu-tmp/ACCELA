package accela.pass.ir.transform.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.recurrence.OnDemandMemoRecurrence;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Builds a bounded memo helper while retaining the source recurrence as an exact fallback. */
public final class Rrt2MemoizationTransform {
  private static final String HELPER_SUFFIX = ".rrt2.memo";
  private static final String GLOBAL_PREFIX = "__rrt2_";

  private Rrt2MemoizationTransform() {}

  public static String helperName(Function function) {
    return function.getName() + HELPER_SUFFIX;
  }

  public static String valuesName(Function function) {
    return GLOBAL_PREFIX + function.getName() + "_values";
  }

  public static String seenName(Function function) {
    return GLOBAL_PREFIX + function.getName() + "_seen";
  }

  /** Generated names are fixed evidence identities; collisions reject instead of changing them. */
  public static boolean symbolsAvailable(
      accela.ir.Module module, OnDemandMemoRecurrence recurrence) {
    Objects.requireNonNull(module, "module");
    Objects.requireNonNull(recurrence, "recurrence");
    List<String> generated = List.of(
        helperName(recurrence.function()),
        valuesName(recurrence.function()),
        seenName(recurrence.function()));
    return module.getFunctions().stream().noneMatch(function -> generated.contains(function.getName()))
        && module.getDeclares().stream().noneMatch(function -> generated.contains(function.getName()))
        && module.getGlobals().stream().noneMatch(global -> generated.contains(global.getName()));
  }

  public static Function apply(
      accela.ir.Module module, OnDemandMemoRecurrence recurrence) {
    Objects.requireNonNull(module, "module");
    Objects.requireNonNull(recurrence, "recurrence");
    if (recurrence.function().getModule() != module) {
      throw new IllegalArgumentException("recurrence function does not belong to module");
    }
    if (!symbolsAvailable(module, recurrence)) {
      throw new IllegalStateException(
          "fixed RRT2 memo helper or storage symbol already exists for @"
              + recurrence.function().getName());
    }

    Type tableType = Type.array(Type.INT, Rrt2MemoizationProfitability.TABLE_CELLS);
    GlobalVariable values = new GlobalVariable(
        valuesName(recurrence.function()), tableType, Constant.zero(tableType), false);
    GlobalVariable seen = new GlobalVariable(
        seenName(recurrence.function()), tableType, Constant.zero(tableType), false);
    module.addGlobal(values);
    module.addGlobal(seen);

    Function helper = new Function(helperName(recurrence.function()), Type.INT);
    helper.addArgument(Type.INT, recurrence.function().getArguments().get(0).getName());
    helper.addArgument(Type.INT, recurrence.function().getArguments().get(1).getName());
    module.addFunction(helper);

    BasicBlock entry = helper.addBlock("rrt2.entry");
    BasicBlock lookup = helper.addBlock("rrt2.lookup");
    BasicBlock hit = helper.addBlock("rrt2.hit");
    BasicBlock miss = helper.addBlock("rrt2.miss");
    BasicBlock fallback = helper.addBlock("rrt2.fallback");
    IRBuilder builder = new IRBuilder(entry);
    Value firstInBounds = stateInBounds(builder, helper.getArguments().get(0));
    Value secondInBounds = stateInBounds(builder, helper.getArguments().get(1));
    Value inDomain = builder.createAnd(firstInBounds, secondInBounds);
    if (recurrence.domainShape()
        == OnDemandMemoRecurrence.DomainShape.TRIANGULAR_NONNEGATIVE) {
      inDomain = builder.createAnd(
          inDomain,
          builder.createICmp(
              "sle", helper.getArguments().get(1), helper.getArguments().get(0)));
    }
    builder.createCondBr(inDomain, lookup, fallback);

    builder.setInsertPoint(lookup);
    Value key = key(builder, helper.getArguments().get(0), helper.getArguments().get(1));
    Value seenAddress = address(builder, tableType, seen, key);
    Value wasSeen = builder.createLoad(Type.INT, seenAddress);
    builder.createCondBr(
        builder.createICmp("ne", wasSeen, Constant.intConst(0)), hit, miss);

    builder.setInsertPoint(hit);
    builder.createRet(builder.createLoad(Type.INT, address(builder, tableType, values, key)));

    builder.setInsertPoint(fallback);
    builder.createRet(builder.createCall(
        recurrence.function(),
        Type.INT,
        helper.getArguments().get(0),
        helper.getArguments().get(1)));

    BasicBlock clonedEntry = cloneOriginalCfg(
        recurrence, helper, tableType, values, seen);
    new IRBuilder(miss).createBr(clonedEntry);
    redirectProgramCallsAndOriginalRecursion(module, recurrence.function(), helper);
    return helper;
  }

  private static BasicBlock cloneOriginalCfg(
      OnDemandMemoRecurrence recurrence,
      Function helper,
      Type tableType,
      GlobalVariable values,
      GlobalVariable seen) {
    Function source = recurrence.function();
    Map<Value, Value> valueMap = new IdentityHashMap<>();
    Map<BasicBlock, BasicBlock> blockMap = new IdentityHashMap<>();
    valueMap.put(source.getArguments().get(0), helper.getArguments().get(0));
    valueMap.put(source.getArguments().get(1), helper.getArguments().get(1));
    for (BasicBlock sourceBlock : source.getBlocks()) {
      BasicBlock copy = helper.addBlock("rrt2.body." + sourceBlock.getLabel());
      blockMap.put(sourceBlock, copy);
      valueMap.put(sourceBlock, copy);
    }

    Map<Instruction, Instruction> instructionMap = new IdentityHashMap<>();
    for (BasicBlock sourceBlock : source.getBlocks()) {
      BasicBlock copyBlock = blockMap.get(sourceBlock);
      for (Instruction sourceInstruction : sourceBlock.getInstructions()) {
        if (sourceInstruction.getOpcode() == Instruction.Opcode.RET) continue;
        Instruction copy = sourceInstruction.copyWithoutOperands();
        if (copy.getOpcode() == Instruction.Opcode.CALL
            && copy.getCallee() == source) copy.setCallee(helper);
        copyBlock.addInstruction(copy);
        valueMap.put(sourceInstruction, copy);
        instructionMap.put(sourceInstruction, copy);
      }
    }
    for (Map.Entry<Instruction, Instruction> entry : instructionMap.entrySet()) {
      Instruction sourceInstruction = entry.getKey();
      Instruction copy = entry.getValue();
      for (int index = 0; index < sourceInstruction.getNumOperands(); index++) {
        Value operand = sourceInstruction.getOperand(index);
        copy.addOperand(valueMap.getOrDefault(operand, operand));
      }
    }
    for (BasicBlock sourceBlock : source.getBlocks()) {
      for (Instruction sourceInstruction : sourceBlock.getInstructions()) {
        if (sourceInstruction.getOpcode() != Instruction.Opcode.RET) continue;
        BasicBlock copyBlock = blockMap.get(sourceBlock);
        IRBuilder builder = new IRBuilder(copyBlock);
        Value result = valueMap.getOrDefault(
            sourceInstruction.getOperand(0), sourceInstruction.getOperand(0));
        Value key = key(builder, helper.getArguments().get(0), helper.getArguments().get(1));
        builder.createStore(result, address(builder, tableType, values, key));
        // Publishing seen after the value store makes zero-initialization and lookup ordering
        // explicit even though SysY execution is single-threaded.
        builder.createStore(
            Constant.intConst(1), address(builder, tableType, seen, key));
        builder.createRet(result);
      }
    }
    return blockMap.get(source.getEntryBlock());
  }

  private static Value stateInBounds(IRBuilder builder, Value state) {
    return builder.createAnd(
        builder.createICmp("sge", state, Constant.intConst(0)),
        builder.createICmp(
            "slt", state,
            Constant.intConst(Rrt2MemoizationProfitability.DOMAIN_EXTENT)));
  }

  private static Value key(IRBuilder builder, Value first, Value second) {
    return builder.createAdd(
        builder.createMul(
            first, Constant.intConst(Rrt2MemoizationProfitability.DOMAIN_EXTENT)),
        second);
  }

  private static Value address(
      IRBuilder builder, Type tableType, GlobalVariable table, Value key) {
    Value wideKey = builder.createSExt(key, Type.I64);
    return builder.createGEP(
        tableType,
        table,
        new Value[] {Constant.int64Const(0), wideKey},
        true);
  }

  private static void redirectProgramCallsAndOriginalRecursion(
      accela.ir.Module module, Function original, Function helper) {
    for (Function function : module.getFunctions()) {
      // The helper's explicit out-of-domain call remains the only call to the source symbol.
      // Repointing the source function's recursive edges to the helper preserves the fallback's
      // original control-flow order while preventing the following production RRT pass from
      // independently tabulating the same recurrence.
      if (function == helper) continue;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() == Instruction.Opcode.CALL
              && instruction.getCallee() == original) instruction.setCallee(helper);
        }
      }
    }
  }
}
