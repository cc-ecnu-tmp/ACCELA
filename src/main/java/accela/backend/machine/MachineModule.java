package accela.backend.machine;

import accela.ir.Function;
import accela.ir.GlobalVariable;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class MachineModule {
  private final accela.ir.Module sourceModule;
  private final List<MachineFunction> functions = new ArrayList<>();
  private final Map<Function, MachineFunction> bySourceFunction = new LinkedHashMap<>();
  private final Map<GlobalVariable, String> globalSymbols = new LinkedHashMap<>();

  public MachineModule(accela.ir.Module sourceModule) {
    this.sourceModule = sourceModule;
    for (GlobalVariable global : sourceModule.getGlobals()) {
      globalSymbols.put(global, global.getName());
    }
  }

  public accela.ir.Module getSourceModule() {
    return sourceModule;
  }

  public void addFunction(Function source, MachineFunction machineFunction) {
    functions.add(machineFunction);
    bySourceFunction.put(source, machineFunction);
  }

  public List<MachineFunction> getFunctions() {
    return Collections.unmodifiableList(functions);
  }

  public String getGlobalSymbol(GlobalVariable global) {
    return globalSymbols.get(global);
  }
}
