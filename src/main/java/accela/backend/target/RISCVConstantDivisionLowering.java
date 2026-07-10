package accela.backend.target;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Expands common signed constant division before register allocation. */
public final class RISCVConstantDivisionLowering {
  public boolean run(MachineFunction function) {
    MachineConstantPool constants = new MachineConstantPool(function);
    Map<MachineInstr, Candidate> candidates = findCandidates(function, constants);
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : List.copyOf(block.getInstructions())) {
        Candidate candidate = candidates.get(instruction);
        if (candidate == null) continue;
        List<MachineInstr> replacement = lower(function, instruction, candidate, constants);
        int index = block.getInstructions().indexOf(instruction);
        block.getInstructions().remove(index);
        block.getInstructions().addAll(index, replacement);
      }
    }
    return !candidates.isEmpty();
  }
  private static Map<MachineInstr, Candidate> findCandidates(
      MachineFunction function, MachineConstantPool constants) {
    Map<MachineInstr, Candidate> result = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        Candidate candidate = candidateFor(instruction);
        if (candidate == null) continue;
        result.put(instruction, candidate);
        constants.count(MachineType.I64, candidate.magic().multiplier());
        if (instruction.getOpcode() == MachineOpcode.REM) {
          constants.count(MachineType.I32, candidate.divisor());
        }
      }
    }
    return result;
  }
  private static Candidate candidateFor(MachineInstr instruction) {
    if (instruction.getType() != MachineType.I32
        || instruction.getOperands().size() != 2
        || instruction.getOpcode() != MachineOpcode.DIV
            && instruction.getOpcode() != MachineOpcode.REM
        || !(instruction.getOperands().get(1) instanceof ImmOperand immediate)) return null;
    long value = immediate.getValue();
    if (value <= 1 || value > (1L << 30) || (value & (value - 1)) == 0) return null;
    SignedDivisionMagic magic = SignedDivisionMagic.forDivisor((int) value);
    return magic.multiplier() < (1L << 31)
        ? new Candidate((int) value, magic) : null;
  }
  private static List<MachineInstr> lower(
      MachineFunction function, MachineInstr original, Candidate candidate,
      MachineConstantPool constants) {
    List<MachineInstr> result = new ArrayList<>();
    MachineOperand numerator = original.getOperands().get(0);
    VRegOperand multiplier = constants.materialize(
        MachineType.I64, candidate.magic().multiplier(), result);
    VirtualRegister product = function.createVirtualRegister(MachineType.I64, "magic.product");
    result.add(binary(MachineOpcode.MUL, product, MachineType.I64, numerator, multiplier));
    VirtualRegister high = function.createVirtualRegister(MachineType.I64, "magic.high");
    result.add(binary(MachineOpcode.ASHR, high, MachineType.I64,
        new VRegOperand(product), new ImmOperand(32 + candidate.magic().postShift())));
    VirtualRegister sign = function.createVirtualRegister(MachineType.I32, "magic.sign");
    result.add(binary(MachineOpcode.ASHR, sign, MachineType.I32,
        numerator, new ImmOperand(31)));
    VirtualRegister quotient = original.getOpcode() == MachineOpcode.DIV
        ? original.getDest() : function.createVirtualRegister(MachineType.I32, "magic.quotient");
    result.add(binary(MachineOpcode.SUB, quotient, MachineType.I32,
        new VRegOperand(high), new VRegOperand(sign)));
    if (original.getOpcode() == MachineOpcode.DIV) return result;
    VRegOperand divisor = constants.materialize(
        MachineType.I32, candidate.divisor(), result);
    VirtualRegister scaled = function.createVirtualRegister(MachineType.I32, "magic.scaled");
    result.add(binary(MachineOpcode.MUL, scaled, MachineType.I32,
        new VRegOperand(quotient), divisor));
    result.add(binary(MachineOpcode.SUB, original.getDest(), MachineType.I32,
        numerator, new VRegOperand(scaled)));
    return result;
  }
  private static MachineInstr binary(
      MachineOpcode opcode, VirtualRegister dest, MachineType type,
      MachineOperand left, MachineOperand right) {
    MachineInstr instruction = new MachineInstr(opcode, dest);
    instruction.setType(type);
    instruction.addOperand(left).addOperand(right);
    return instruction;
  }
  private record Candidate(int divisor, SignedDivisionMagic magic) {}
}
