package accela.backend.frame;

import accela.backend.machine.MachineType;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class MachineFrameInfo {
  private final List<StackSlot> slots = new ArrayList<>();
  private int nextSlotId = 0;
  private int outgoingArgBytes = 0;
  private boolean hasCalls = false;
  private int saveS0Offset = -1;
  private int saveRaOffset = -1;
  private int frameSize = 0;

  public StackSlot createLocalSlot(MachineType type, int size, int align) {
    StackSlot slot = new StackSlot(nextSlotId++, StackSlotKind.LOCAL, type, size, align);
    slots.add(slot);
    return slot;
  }

  public StackSlot createSpillSlot(MachineType type, int size, int align) {
    StackSlot slot = new StackSlot(nextSlotId++, StackSlotKind.SPILL, type, size, align);
    slots.add(slot);
    return slot;
  }

  public List<StackSlot> getSlots() {
    return Collections.unmodifiableList(slots);
  }

  public void markHasCall() {
    hasCalls = true;
  }

  public boolean hasCalls() {
    return hasCalls;
  }

  public void reserveOutgoingArgBytes(int bytes) {
    outgoingArgBytes = Math.max(outgoingArgBytes, bytes);
  }

  public int getOutgoingArgBytes() {
    return outgoingArgBytes;
  }

  public int getSaveS0Offset() {
    return saveS0Offset;
  }

  public int getSaveRaOffset() {
    return saveRaOffset;
  }

  public int getFrameSize() {
    return frameSize;
  }

  public void finalizeLayout(RISCVTarget target) {
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
