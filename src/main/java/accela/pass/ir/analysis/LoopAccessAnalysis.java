package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import accela.pass.ir.analysis.scev.SCEV;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/**
 * Describes loop memory accesses without making transformation-specific alias assumptions.
 *
 * <p>SCEV and provenance facts are retained even when the current runtime-versioning transform
 * cannot encode a sound range check for them.
 */
public final class LoopAccessAnalysis
    implements FunctionAnalysis<LoopAccessAnalysis.Result> {
  public record Access(
      Instruction instruction,
      MemoryLocation location,
      SCEV address,
      Value provenanceRoot,
      boolean loopInvariant,
      boolean write) {}

  public record LoopAccessInfo(
      LoopAnalysis.Loop loop,
      List<Access> loads,
      List<Access> stores,
      List<Instruction> calls) {
    public LoopAccessInfo {
      loads = List.copyOf(loads);
      stores = List.copyOf(stores);
      calls = List.copyOf(calls);
    }

    public boolean callMayWrite(Access access, GlobalModRefAnalysis.Result modRef) {
      return calls.stream().anyMatch(
          call -> modRef == null || modRef.mayWrite(call, access.location().pointer()));
    }
  }

  public static final class Result {
    private final Map<LoopAnalysis.Loop, LoopAccessInfo> byLoop;

    private Result(Map<LoopAnalysis.Loop, LoopAccessInfo> byLoop) {
      this.byLoop = Map.copyOf(byLoop);
    }

    public LoopAccessInfo getInfo(LoopAnalysis.Loop loop) {
      return byLoop.get(loop);
    }

    public List<LoopAccessInfo> loops() {
      return List.copyOf(byLoop.values());
    }
  }

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    LoopAnalysis.Result loops = fam.getResult(LoopAnalysis.class, function);
    ScalarEvolutionAnalysis.Result scev =
        fam.getResult(ScalarEvolutionAnalysis.class, function);
    Map<LoopAnalysis.Loop, LoopAccessInfo> result = new IdentityHashMap<>();
    for (LoopAnalysis.Loop loop : loops.loops()) {
      List<Access> loads = new ArrayList<>();
      List<Access> stores = new ArrayList<>();
      List<Instruction> calls = new ArrayList<>();
      for (BasicBlock block : loop.blocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() == Instruction.Opcode.CALL) {
            calls.add(instruction);
            continue;
          }
          MemoryLocation location = MemoryLocation.fromInstruction(instruction);
          if (location == null) continue;
          SCEV address = scev.getSCEV(location.pointer());
          Access access =
              new Access(
                  instruction,
                  location,
                  address,
                  PointerProvenance.root(location.pointer()),
                  scev.isLoopInvariant(address, loop),
                  instruction.getOpcode() == Instruction.Opcode.STORE);
          (access.write() ? stores : loads).add(access);
        }
      }
      result.put(loop, new LoopAccessInfo(loop, loads, stores, calls));
    }
    return new Result(result);
  }
}
