package accela.backend.frame;

import accela.backend.machine.MachineType;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class MachineFrameInfo {
  private final List<StackSlot> slots = new ArrayList<>();
  private int nextSlotId = 0;
  private int outgoingArgBytes = 0;
  private boolean hasCalls = false;
  private int saveRaOffset = -1;
  private final Map<String, Integer> calleeSavedOffsets = new LinkedHashMap<>();
  private final Map<String, Integer> floatCalleeSavedOffsets = new LinkedHashMap<>();
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

  public int getSaveRaOffset() {
    return saveRaOffset;
  }

  public void markCalleeSavedRegister(String register) {
    calleeSavedOffsets.putIfAbsent(register, -1);
  }

  public Map<String, Integer> getCalleeSavedOffsets() {
    return Collections.unmodifiableMap(calleeSavedOffsets);
  }

  public void markFloatCalleeSavedRegister(String register) {
    floatCalleeSavedOffsets.putIfAbsent(register, -1);
  }

  public Map<String, Integer> getFloatCalleeSavedOffsets() {
    return Collections.unmodifiableMap(floatCalleeSavedOffsets);
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
    if (hasCalls) {
      offset = target.alignTo(offset, target.stackAlignOf(MachineType.PTR));
      saveRaOffset = offset;
      offset += target.stackSizeOf(MachineType.PTR);
    }
    for (String register : calleeSavedOffsets.keySet()) {
      offset = target.alignTo(offset, target.stackAlignOf(MachineType.PTR));
      calleeSavedOffsets.put(register, offset);
      offset += target.stackSizeOf(MachineType.PTR);
    }
    for (String register : floatCalleeSavedOffsets.keySet()) {
      offset = target.alignTo(offset, target.stackAlignOf(MachineType.I64));
      floatCalleeSavedOffsets.put(register, offset);
      offset += target.stackSizeOf(MachineType.I64);
    }
    frameSize = target.alignTo(offset, 16);
  }
}
