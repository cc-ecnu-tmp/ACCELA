package accela.pass.ir.transform.region;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.FunctionMemorySummary;
import accela.pass.ir.analysis.alias.PointerProvenance;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Forwards values through a single basic block when SysY provenance and call effects prove the
 * memory location unchanged. Unknown pointers and unknown calls invalidate the local table.
 */
public final class SysYRegionMemoryForwarding {
  private SysYRegionMemoryForwarding() {}

  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;
    private accela.ir.Module cachedModule;
    private GlobalModRefAnalysis.Result modRef;

    public Pass(PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = instrumentation;
      this.descriptor = descriptor;
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (function.getModule() != cachedModule) {
        cachedModule = function.getModule();
        modRef = cachedModule == null ? null : GlobalModRefAnalysis.analyze(cachedModule);
      }
      PassDecisionEmitter decision = instrumentation.decisionEmitter(
          descriptor, occurrence, "function", "@" + function.getName());
      decision.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      Result result = forward(function);
      if (result.changed) {
        decision.applied(DecisionReasonCode.APPLIED_PROFITABLE);
        return PreservedAnalyses.none();
      }
      decision.rejectedLegality(result.rejectionObligation);
      return PreservedAnalyses.all();
    }

    private Result forward(Function function) {
      boolean sawExactRegion = false;
      boolean changed = false;
      for (BasicBlock block : function.getBlocks()) {
        Map<Value, Value> known = new LinkedHashMap<>();
        for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
          switch (instruction.getOpcode()) {
            case LOAD -> {
              Value pointer = instruction.getOperand(0);
              if (PointerProvenance.analyze(pointer).exact()) {
                sawExactRegion = true;
                Value replacement = findKnown(known, pointer);
              if (replacement != null && replacement.getType() == instruction.getType()) {
                  instruction.replaceAllUsesWith(replacement);
                  instruction.eraseFromParent();
                  changed = true;
                } else {
                  known.put(pointer, instruction);
                }
              } else {
                known.clear();
              }
            }
            case STORE -> {
              Value pointer = instruction.getOperand(1);
              if (!PointerProvenance.analyze(pointer).exact()) {
                known.clear();
                continue;
              }
              sawExactRegion = true;
              invalidate(known, pointer);
              known.put(pointer, instruction.getOperand(0));
            }
            case CALL -> {
              if (modRef == null) {
                known.clear();
              } else {
                FunctionMemorySummary summary = modRef.summary(function);
                known.entrySet().removeIf(entry ->
                    summary.mayRead(instruction, entry.getKey())
                        || summary.mayWrite(instruction, entry.getKey()));
              }
            }
            default -> {}
          }
        }
      }
      if (changed) return Result.applied();
      if (!sawExactRegion) return Result.rejected("candidate.sysy-region-memory-forwarding.object-provenance");
      return Result.rejected("candidate.sysy-region-memory-forwarding.profitability");
    }

    private static Value findKnown(Map<Value, Value> known, Value pointer) {
      Value match = null;
      for (Map.Entry<Value, Value> entry : known.entrySet()) {
        if (!PointerProvenance.mayAliasWithRegions(entry.getKey(), pointer)) continue;
        if (PointerProvenance.analyze(entry.getKey()).region().equals(
            PointerProvenance.analyze(pointer).region())) {
          match = entry.getValue();
        }
      }
      return match;
    }

    private static void invalidate(Map<Value, Value> known, Value pointer) {
      known.keySet().removeIf(existing -> PointerProvenance.mayAliasWithRegions(existing, pointer));
    }

    private record Result(boolean changed, String rejectionObligation) {
      static Result applied() { return new Result(true, null); }
      static Result rejected(String obligation) { return new Result(false, obligation); }
    }
  }
}
