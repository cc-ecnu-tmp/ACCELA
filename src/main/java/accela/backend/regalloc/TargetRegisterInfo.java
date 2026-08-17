package accela.backend.regalloc;

import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.RegisterClass;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.HashSet;
import java.util.Collections;
import java.util.List;
import java.util.Set;

public final class TargetRegisterInfo {
  private final boolean hasVectorRegisters;

  public TargetRegisterInfo() {
    this.hasVectorRegisters = false;
  }

  public TargetRegisterInfo(RISCVTarget target) {
    this.hasVectorRegisters = target.hasRVV();
  }
  private static final List<PhysicalRegister> ALL_INT_REGISTERS =
      List.of(
          reg("t0", MachineType.I32),
          reg("t1", MachineType.I32),
          reg("t2", MachineType.I32),
          reg("t3", MachineType.I32),
          reg("t4", MachineType.I32),
          reg("t5", MachineType.I32),
          reg("t6", MachineType.I32),
          reg("a0", MachineType.I32),
          reg("a1", MachineType.I32),
          reg("a2", MachineType.I32),
          reg("a3", MachineType.I32),
          reg("a4", MachineType.I32),
          reg("a5", MachineType.I32),
          reg("a6", MachineType.I32),
          reg("a7", MachineType.I32),
          reg("s0", MachineType.I32),
          reg("s1", MachineType.I32),
          reg("s2", MachineType.I32),
          reg("s3", MachineType.I32),
          reg("s4", MachineType.I32),
          reg("s5", MachineType.I32),
          reg("s6", MachineType.I32),
          reg("s7", MachineType.I32),
          reg("s8", MachineType.I32),
          reg("s9", MachineType.I32),
          reg("s10", MachineType.I32),
          reg("s11", MachineType.I32));

  private static final List<PhysicalRegister> ALL_FLOAT_REGISTERS =
      List.of(
          reg("ft0", MachineType.F32),
          reg("ft1", MachineType.F32),
          reg("ft2", MachineType.F32),
          reg("ft3", MachineType.F32),
          reg("ft4", MachineType.F32),
          reg("ft5", MachineType.F32),
          reg("ft6", MachineType.F32),
          reg("ft7", MachineType.F32),
          reg("ft8", MachineType.F32),
          reg("ft9", MachineType.F32),
          reg("ft10", MachineType.F32),
          reg("ft11", MachineType.F32),
          reg("fa0", MachineType.F32),
          reg("fa1", MachineType.F32),
          reg("fa2", MachineType.F32),
          reg("fa3", MachineType.F32),
          reg("fa4", MachineType.F32),
          reg("fa5", MachineType.F32),
          reg("fa6", MachineType.F32),
          reg("fa7", MachineType.F32),
          reg("fs0", MachineType.F32),
          reg("fs1", MachineType.F32),
          reg("fs2", MachineType.F32),
          reg("fs3", MachineType.F32),
          reg("fs4", MachineType.F32),
          reg("fs5", MachineType.F32),
          reg("fs6", MachineType.F32),
          reg("fs7", MachineType.F32),
          reg("fs8", MachineType.F32),
          reg("fs9", MachineType.F32),
          reg("fs10", MachineType.F32),
          reg("fs11", MachineType.F32));

  private static final Set<String> RESERVED =
      Set.of("zero", "sp", "gp", "tp", "ra", "fp");

  private static final Set<String> CALL_ARGUMENT_REGISTERS =
      Set.of(
          "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7",
          "fa0", "fa1", "fa2", "fa3", "fa4", "fa5", "fa6", "fa7");

  private static final Set<String> EMITTER_SCRATCH_REGISTERS =
      Set.of("a4", "a5", "a6", "a7", "fa4", "fa5", "fa6", "fa7");

  private static final Set<String> INT_CALLER_SAVED =
      Set.of(
          "t0", "t1", "t2", "t3", "t4", "t5", "t6",
          "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7");

  private static final Set<String> INT_CALLEE_SAVED =
      Set.of("s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11");

  private static final Set<String> FLOAT_CALLER_SAVED =
      Set.of(
          "ft0", "ft1", "ft2", "ft3", "ft4", "ft5", "ft6", "ft7", "ft8", "ft9", "ft10", "ft11",
          "fa0", "fa1", "fa2", "fa3", "fa4", "fa5", "fa6", "fa7");

  private static final Set<String> FLOAT_CALLEE_SAVED =
      Set.of("fs0", "fs1", "fs2", "fs3", "fs4", "fs5", "fs6", "fs7", "fs8", "fs9", "fs10", "fs11");

  private static final List<PhysicalRegister> INT_REGISTERS =
      ALL_INT_REGISTERS.stream().filter(register -> !isAllocatorReservedName(register.getName())).toList();

  private static final List<PhysicalRegister> FLOAT_REGISTERS =
      ALL_FLOAT_REGISTERS.stream().filter(register -> !isAllocatorReservedName(register.getName())).toList();

  private static final List<PhysicalRegister> INT_NON_ARGUMENT_REGISTERS =
      INT_REGISTERS.stream().filter(register -> !isArgumentRegisterName(register.getName())).toList();

  private static final List<PhysicalRegister> FLOAT_NON_ARGUMENT_REGISTERS =
      FLOAT_REGISTERS.stream().filter(register -> !isArgumentRegisterName(register.getName())).toList();

  private static final List<PhysicalRegister> INT_CALLEE_SAVED_REGISTERS =
      INT_REGISTERS.stream().filter(TargetRegisterInfo::isCalleeSavedName).toList();

  private static final List<PhysicalRegister> FLOAT_CALLEE_SAVED_REGISTERS =
      FLOAT_REGISTERS.stream().filter(TargetRegisterInfo::isCalleeSavedName).toList();

  public List<PhysicalRegister> allocatableRegisters(MachineType type) {
    if (type.isFloat()) {
      return FLOAT_REGISTERS;
    }
    if (type.isIntegerLike()) {
      return INT_REGISTERS;
    }
    return Collections.emptyList();
  }

  public List<PhysicalRegister> allocatableRegisters(VirtualRegister register) {
    if (!register.getType().isVector()) return allocatableRegisters(register.getType());
    if (!hasVectorRegisters) return Collections.emptyList();
    return vectorGroups(register.getVectorShape().lmul());
  }

  public int registerCount(MachineType type) {
    return allocatableRegisters(type).size();
  }

  public List<PhysicalRegister> nonArgumentRegisters(MachineType type) {
    if (type.isFloat()) return FLOAT_NON_ARGUMENT_REGISTERS;
    if (type.isIntegerLike()) return INT_NON_ARGUMENT_REGISTERS;
    return Collections.emptyList();
  }

  public List<PhysicalRegister> nonArgumentRegisters(VirtualRegister register) {
    return allocatableRegisters(register);
  }

  public List<PhysicalRegister> callerSavedRegisters(MachineType type) {
    return allocatableRegisters(type).stream().filter(this::isCallerSaved).toList();
  }

  public List<PhysicalRegister> callerSavedRegisters(VirtualRegister register) {
    if (register.getType().isVector()) return allocatableRegisters(register);
    return callerSavedRegisters(register.getType());
  }

  public List<PhysicalRegister> calleeSavedRegisters(MachineType type) {
    if (type.isFloat()) {
      return FLOAT_CALLEE_SAVED_REGISTERS;
    }
    if (type.isIntegerLike()) {
      return INT_CALLEE_SAVED_REGISTERS;
    }
    return Collections.emptyList();
  }

  public List<PhysicalRegister> calleeSavedRegisters(VirtualRegister register) {
    if (register.getType().isVector()) return Collections.emptyList();
    return calleeSavedRegisters(register.getType());
  }

  public boolean isReserved(PhysicalRegister register) {
    return RESERVED.contains(register.getName());
  }

  public boolean isAllocatorReserved(PhysicalRegister register) {
    return isAllocatorReservedName(register.getName());
  }

  public boolean isCallerSaved(PhysicalRegister register) {
    if (register.getRegisterClass() == RegisterClass.VR) return true;
    String name = register.getName();
    return INT_CALLER_SAVED.contains(name) || FLOAT_CALLER_SAVED.contains(name);
  }

  public boolean isCalleeSaved(PhysicalRegister register) {
    String name = register.getName();
    return INT_CALLEE_SAVED.contains(name) || FLOAT_CALLEE_SAVED.contains(name);
  }

  private static PhysicalRegister reg(String name, MachineType type) {
    return new PhysicalRegister(name, type);
  }

  private static boolean isCalleeSavedName(PhysicalRegister register) {
    String name = register.getName();
    return INT_CALLEE_SAVED.contains(name) || FLOAT_CALLEE_SAVED.contains(name);
  }

  private static boolean isAllocatorReservedName(String name) {
    return RESERVED.contains(name)
        || EMITTER_SCRATCH_REGISTERS.contains(name);
  }

  private static boolean isArgumentRegisterName(String name) {
    return CALL_ARGUMENT_REGISTERS.contains(name);
  }

  private static List<PhysicalRegister> vectorGroups(int lmul) {
    java.util.ArrayList<PhysicalRegister> groups = new java.util.ArrayList<>();
    for (int base = 0; base + lmul <= 32; base += lmul) {
      // v0 is the dedicated execution-mask scratch and is not allocator-visible.
      if (base == 0) continue;
      Set<Integer> units = new HashSet<>();
      for (int index = 0; index < lmul; index++) units.add(base + index);
      groups.add(
          new PhysicalRegister(
              "v" + base,
              MachineType.VECTOR,
              RegisterClass.VR,
              base,
              units));
    }
    return List.copyOf(groups);
  }
}
