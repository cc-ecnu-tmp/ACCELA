package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Top-level container for the structured IR.
 *
 * <p>A module owns three kinds of top-level entities:
 *
 * <p>- global variables/constants
 *
 * <p>- defined functions
 *
 * <p>- external declarations for runtime/library calls
 *
 * <p>This is the handoff object between the frontend/optimization pipeline and later IR consumers
 * such as the IR printer and backend lowering.
 */
public class Module {
  private final List<GlobalVariable> globals = new ArrayList<>();
  private final List<Function> functions = new ArrayList<>();
  private final List<Function> declares = new ArrayList<>();

  /** Adds a defined global object to the module. */
  public void addGlobal(GlobalVariable gv) {
    globals.add(gv);
  }

  /** Adds a full function definition and records the owning module on the function. */
  public void addFunction(Function func) {
    func.setParent(this);
    functions.add(func);
  }

  public void removeFunction(Function function) {
    if (!functions.remove(function)) {
      throw new IllegalArgumentException("function does not belong to this module");
    }
    function.setParent(null);
  }

  /** Adds an external function declaration without a body. */
  public void addDeclare(Function func) {
    declares.add(func);
  }

  /** Returns module-level global objects in insertion order. */
  public List<GlobalVariable> getGlobals() {
    return Collections.unmodifiableList(globals);
  }

  /** Returns function definitions in insertion order. */
  public List<Function> getFunctions() {
    return Collections.unmodifiableList(functions);
  }

  /** Returns external declarations used by this module. */
  public List<Function> getDeclares() {
    return Collections.unmodifiableList(declares);
  }
}
