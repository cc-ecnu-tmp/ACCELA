package accela.backend.frame;

import accela.backend.machine.MachineType;

public final class StackSlot {
  private final int id;
  private final StackSlotKind kind;
  private final MachineType type;
  private final int size;
  private final int align;
  private int offset = -1;

  public StackSlot(int id, StackSlotKind kind, MachineType type, int size, int align) {
    this.id = id;
    this.kind = kind;
    this.type = type;
    this.size = size;
    this.align = align;
  }

  public int getId() {
    return id;
  }

  public StackSlotKind getKind() {
    return kind;
  }

  public MachineType getType() {
    return type;
  }

  public int getSize() {
    return size;
  }

  public int getAlign() {
    return align;
  }

  public int getOffset() {
    return offset;
  }

  public void setOffset(int offset) {
    this.offset = offset;
  }
}
