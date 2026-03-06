package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public class Module {
  private final List<GlobalVariable> globals = new ArrayList<>();
  private final List<Function> functions = new ArrayList<>();
  private final List<Function> declares = new ArrayList<>();

  public void addGlobal(GlobalVariable gv) {
    globals.add(gv);
  }

  public void addFunction(Function func) {
    func.setParent(this);
    functions.add(func);
  }

  public void addDeclare(Function func) {
    declares.add(func);
  }

  public List<GlobalVariable> getGlobals() {
    return Collections.unmodifiableList(globals);
  }

  public List<Function> getFunctions() {
    return Collections.unmodifiableList(functions);
  }

  public List<Function> getDeclares() {
    return Collections.unmodifiableList(declares);
  }
}
