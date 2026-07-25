package accela.backend.frame;

import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegister;
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
  private int saveS0Offset = -1;
  private int saveRaOffset = -1;
  private final Map<String, CalleeSavedRegisterSave> calleeSavedRegisterSaves = new LinkedHashMap<>();
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

  public void addCalleeSavedRegister(PhysicalRegister register) {
    calleeSavedRegisterSaves.computeIfAbsent(
        register.getName(), ignored -> new CalleeSavedRegisterSave(register));
  }

  public List<CalleeSavedRegisterSave> getCalleeSavedRegisterSaves() {
    return Collections.unmodifiableList(new ArrayList<>(calleeSavedRegisterSaves.values()));
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
    for (CalleeSavedRegisterSave save : calleeSavedRegisterSaves.values()) {
      MachineType saveType = save.getRegister().getType().isFloat() ? MachineType.F32 : MachineType.PTR;
      offset = target.alignTo(offset, target.stackAlignOf(saveType));
      save.setOffset(offset);
      offset += target.stackSizeOf(saveType);
    }
    frameSize = target.alignTo(offset, 16);
  }

  public static final class CalleeSavedRegisterSave {
    private final PhysicalRegister register;
    private int offset = -1;

    private CalleeSavedRegisterSave(PhysicalRegister register) {
      this.register = register;
    }

    public PhysicalRegister getRegister() {
      return register;
    }

    public int getOffset() {
      return offset;
    }

    private void setOffset(int offset) {
      this.offset = offset;
    }
  }
}
