package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public abstract class Value {
  protected Type type;
  protected String name;
  private final List<Use> uses = new ArrayList<>();

  protected Value(Type type, String name) {
    this.type = type;
    this.name = name;
  }

  public Type getType() {
    return type;
  }

  public String getName() {
    return name;
  }

  public void setName(String name) {
    this.name = name;
  }

  void addUse(Use use) {
    uses.add(use);
  }

  void removeUse(Use use) {
    uses.remove(use);
  }

  public List<Use> getUses() {
    return Collections.unmodifiableList(uses);
  }

  public boolean hasUses() {
    return !uses.isEmpty();
  }

  public int getNumUses() {
    return uses.size();
  }

  public void replaceAllUsesWith(Value newValue) {
    List<Use> snapshot = new ArrayList<>(uses);
    for (Use use : snapshot) {
      use.setValue(newValue);
    }
  }
}
