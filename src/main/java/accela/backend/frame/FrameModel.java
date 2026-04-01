package accela.backend;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

enum StackSlotKind {
  LOCAL,
  SPILL
}

final class StackSlot {
  private final int id;
  private final StackSlotKind kind;
  private final MachineType type;
  private final int size;
  private final int align;
  private int offset = -1;

  StackSlot(int id, StackSlotKind kind, MachineType type, int size, int align) {
    this.id = id;
    this.kind = kind;
    this.type = type;
    this.size = size;
    this.align = align;
  }

  int getId() {
    return id;
  }

  StackSlotKind getKind() {
    return kind;
  }

  MachineType getType() {
    return type;
  }

  int getSize() {
    return size;
  }

  int getAlign() {
    return align;
  }

  int getOffset() {
    return offset;
  }

  void setOffset(int offset) {
    this.offset = offset;
  }
}

final class MachineFrameInfo {
  private final List<StackSlot> slots = new ArrayList<>();
  private int nextSlotId = 0;
  private int outgoingArgBytes = 0;
  private boolean hasCalls = false;
  private int saveS0Offset = -1;
  private int saveRaOffset = -1;
  private int frameSize = 0;

  StackSlot createLocalSlot(MachineType type, int size, int align) {
    StackSlot slot = new StackSlot(nextSlotId++, StackSlotKind.LOCAL, type, size, align);
    slots.add(slot);
    return slot;
  }

  StackSlot createSpillSlot(MachineType type, int size, int align) {
    StackSlot slot = new StackSlot(nextSlotId++, StackSlotKind.SPILL, type, size, align);
    slots.add(slot);
    return slot;
  }

  List<StackSlot> getSlots() {
    return Collections.unmodifiableList(slots);
  }

  void markHasCall() {
    hasCalls = true;
  }

  boolean hasCalls() {
    return hasCalls;
  }

  void reserveOutgoingArgBytes(int bytes) {
    outgoingArgBytes = Math.max(outgoingArgBytes, bytes);
  }

  int getOutgoingArgBytes() {
    return outgoingArgBytes;
  }

  int getSaveS0Offset() {
    return saveS0Offset;
  }

  int getSaveRaOffset() {
    return saveRaOffset;
  }

  int getFrameSize() {
    return frameSize;
  }

  void finalizeLayout(RISCVTarget target) {
    int offset = outgoingArgBytes;
    for (StackSlot slot : slots) {
      offset = target.alignTo(offset, slot.getAlign());
      slot.setOffset(offset);
      offset += slot.getSize();
    }
    offset = target.alignTo(offset, target.stackAlignOf(MachineType.PTR));
    saveS0Offset = offset;
    offset += target.stackSizeOf(MachineType.PTR);
    if (hasCalls) {
      offset = target.alignTo(offset, target.stackAlignOf(MachineType.PTR));
      saveRaOffset = offset;
      offset += target.stackSizeOf(MachineType.PTR);
    }
    frameSize = target.alignTo(offset, 16);
  }
}
