package accela.pass.ir.analysis.alias;

import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.Objects;

/**
 * Stable function-level memory contract used by SysY candidates.
 *
 * <p>The summary is deliberately conservative: callers can only rely on a pointer argument when
 * the underlying whole-module ModRef result proves that an access is absent.  Unknown external
 * calls therefore remain read/write barriers instead of being silently treated as pure.
 */
public final class FunctionMemorySummary {
  private final GlobalModRefAnalysis.Result modRef;
  private final Function function;

  private FunctionMemorySummary(GlobalModRefAnalysis.Result modRef, Function function) {
    this.modRef = Objects.requireNonNull(modRef, "modRef");
    this.function = Objects.requireNonNull(function, "function");
  }

  public static FunctionMemorySummary forFunction(
      GlobalModRefAnalysis.Result modRef, Function function) {
    return new FunctionMemorySummary(modRef, function);
  }

  public boolean mayRead(Instruction call, Value pointer) {
    return modRef.mayRead(call, pointer);
  }

  public boolean mayWrite(Instruction call, Value pointer) {
    return modRef.mayWrite(call, pointer);
  }

  public boolean isPure(Instruction call) {
    return modRef.isPure(call);
  }

  public boolean readsArgument(int index) {
    return modRef.readsArgument(function, index);
  }

  public boolean writesArgument(int index) {
    return modRef.writesArgument(function, index);
  }

  public boolean escapesArgument(int index) {
    return modRef.escapesArgument(function, index);
  }

  public boolean isReadonlyArgument(int index) {
    return !writesArgument(index) && !escapesArgument(index);
  }

  public boolean hasUnknownEffects() {
    return modRef.hasUnknownEffects(function);
  }

  public Function function() {
    return function;
  }
}
