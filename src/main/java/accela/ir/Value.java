package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Base class for all typed IR entities that can appear as operands.
 *
 * <p>Instructions, constants, function arguments, basic blocks, functions, and globals all inherit
 * from {@code Value}. The shared responsibilities here are:
 *
 * <p>- carrying an IR type
 *
 * <p>- optionally carrying a symbolic name
 *
 * <p>- maintaining the use-list needed for SSA rewrites and local transformations
 */
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

  /** Updates the symbolic/debug name associated with this value. */
  public void setName(String name) {
    this.name = name;
  }

  /** Records that some instruction operand now references this value. */
  void addUse(Use use) {
    uses.add(use);
  }

  /** Removes a previously registered use from the use-list. */
  void removeUse(Use use) {
    uses.remove(use);
  }

  /** Returns the current use-list snapshot for this value. */
  public List<Use> getUses() {
    return Collections.unmodifiableList(uses);
  }

  public boolean hasUses() {
    return !uses.isEmpty();
  }

  public int getNumUses() {
    return uses.size();
  }

  /**
   * Replaces every operand that references this value with {@code newValue}.
   *
   * <p>This is the primitive used by many simple IR rewrites, including mem2reg promotion.
   */
  public void replaceAllUsesWith(Value newValue) {
    List<Use> snapshot = new ArrayList<>(uses);
    for (Use use : snapshot) {
      use.setValue(newValue);
    }
  }
}
