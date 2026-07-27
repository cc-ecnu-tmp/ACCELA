package accela.backend.lowering.globalmerge;

import accela.backend.target.RISCVTarget;
import accela.ir.GlobalVariable;
import accela.ir.Module;
import java.util.LinkedHashMap;
import java.util.Map;

/** Describes the exact offsets of globals emitted into each assembly section. */
final class GlobalLayout {
  private final Map<String, Location> locations = new LinkedHashMap<>();

  GlobalLayout(Module module, RISCVTarget target) {
    Section writable = new Section();
    Section readOnly = new Section();
    for (GlobalVariable global : module.getGlobals()) {
      Section section = global.isConstant() ? readOnly : writable;
      if (section.base == null) section.base = global.getName();
      locations.put(global.getName(), new Location(section.base, section.size));
      section.size += target.sizeOfIrType(global.getValueType());
    }
  }

  Location locationOf(String symbol) {
    return locations.get(symbol);
  }

  record Location(String base, int offset) {}

  private static final class Section {
    private String base;
    private int size;
  }
}
