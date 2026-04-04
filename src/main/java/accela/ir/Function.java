package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * An IR function definition or declaration.
 *
 * Extends Value so it can be referenced in call instructions.
 */
public class Function extends Value {
  private final Type returnType;
  private final List<Argument> arguments = new ArrayList<>();
  private final List<BasicBlock> blocks = new ArrayList<>();
  private Module parent;

  public Function(String name, Type returnType) {
    super(Type.PTR, name); // functions have pointer type in opaque-ptr world
    this.returnType = returnType;
  }

  public Argument addArgument(Type type, String name) {
    Argument arg = new Argument(type, name, this, arguments.size());
    arguments.add(arg);
    return arg;
  }

  public List<Argument> getArguments() {
    return Collections.unmodifiableList(arguments);
  }

  public int getNumArgs() {
    return arguments.size();
  }

  public BasicBlock addBlock(String label) {
    BasicBlock bb = new BasicBlock(label);
    bb.setParent(this);
    blocks.add(bb);
    return bb;
  }

  public List<BasicBlock> getBlocks() {
    return Collections.unmodifiableList(blocks);
  }

  public BasicBlock getEntryBlock() {
    return blocks.isEmpty() ? null : blocks.get(0);
  }

  /** Detaches a basic block from this function (instructions must be cleared separately). */
  public void removeBlock(BasicBlock bb) {
    if (!blocks.remove(bb)) {
      throw new IllegalArgumentException("basic block does not belong to this function");
    }
    bb.setParent(null);
  }

  public Type getReturnType() {
    return returnType;
  }

  public Module getModule() {
    return parent;
  }

  void setParent(Module module) {
    this.parent = module;
  }

  /**
   * A function parameter. Extends Value so it can be used as an operand
   * in instructions within the function body.
   */
  public static class Argument extends Value {
    private final Function parent;
    private final int argNo;

    Argument(Type type, String name, Function parent, int argNo) {
      super(type, name);
      this.parent = parent;
      this.argNo = argNo;
    }

    public Function getParent() {
      return parent;
    }

    public int getArgNo() {
      return argNo;
    }
  }
}
