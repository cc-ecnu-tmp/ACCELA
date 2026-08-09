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

  public int getSaveRaOffset() {
    return saveRaOffset;
  }

  public int getFrameSize() {
    return frameSize;
  }

  public boolean addCalleeSavedRegister(PhysicalRegister register) {
    if (calleeSavedRegisterSaves.containsKey(register.getName())) return false;
    calleeSavedRegisterSaves.put(register.getName(), new CalleeSavedRegisterSave(register));
    return true;
  }

  public List<CalleeSavedRegisterSave> getCalleeSavedRegisterSaves() {
    return Collections.unmodifiableList(new ArrayList<>(calleeSavedRegisterSaves.values()));
  }

  public boolean finalizeLayout(RISCVTarget target) {
    boolean changed = false;
    int offset = outgoingArgBytes;
    for (StackSlot slot : slots) {
      offset = target.alignTo(offset, slot.getAlign());
      changed |= slot.getOffset() != offset;
      slot.setOffset(offset);
      offset += slot.getSize();
    }
    if (hasCalls) {
      offset = target.alignTo(offset, target.stackAlignOf(MachineType.PTR));
      changed |= saveRaOffset != offset;
      saveRaOffset = offset;
      offset += target.stackSizeOf(MachineType.PTR);
    }
    for (CalleeSavedRegisterSave save : calleeSavedRegisterSaves.values()) {
      MachineType saveType = save.getRegister().getType().isFloat() ? MachineType.I64 : MachineType.PTR;
      offset = target.alignTo(offset, target.stackAlignOf(saveType));
      changed |= save.getOffset() != offset;
      save.setOffset(offset);
      offset += target.stackSizeOf(saveType);
    }
    int finalizedFrameSize = target.alignTo(offset, 16);
    changed |= frameSize != finalizedFrameSize;
    frameSize = finalizedFrameSize;
    return changed;
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
