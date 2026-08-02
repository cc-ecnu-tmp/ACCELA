package accela.backend.target;

import accela.backend.machine.MachineOpcode;
import java.util.List;

/** Selects short constant-arithmetic sequences using allocator-reserved scratch registers. */
final class RISCVStrengthReduction {
  private final String temporary;

  RISCVStrengthReduction(String temporary) {
    this.temporary = temporary;
  }

  boolean emit(
      MachineOpcode opcode, long value, String lhs, String dst, boolean wordResult,
      List<String> lines) {
    if (opcode == MachineOpcode.MUL)
      return emitMultiply(value, lhs, dst, wordResult, lines);
    if (!wordResult || value > Integer.MAX_VALUE || !isPowerOfTwo(value)) return false;
    int shift = Long.numberOfTrailingZeros(value);
    if (opcode == MachineOpcode.DIV) {
      if (shift == 0) {
        lines.add("  addiw " + dst + ", " + lhs + ", 0");
      } else {
        emitBias(lhs, dst, shift, lines);
        lines.add("  sraiw " + dst + ", " + dst + ", " + shift);
      }
      return true;
    }
    return false;
  }

  /** Biases negative dividends by 2^shift - 1 so shifting rounds toward zero. */
  private void emitBias(String lhs, String dst, int shift, List<String> lines) {
    if (shift == 1) {
      lines.add("  srliw " + temporary + ", " + lhs + ", 31");
      lines.add("  addw " + dst + ", " + lhs + ", " + temporary);
      return;
    }
    lines.add("  sraiw " + temporary + ", " + lhs + ", " + (Integer.SIZE - 1));
    lines.add("  srliw " + temporary + ", " + temporary + ", " + (Integer.SIZE - shift));
    lines.add("  addw " + dst + ", " + lhs + ", " + temporary);
  }

  private boolean emitMultiply(
      long value, String lhs, String dst, boolean wordResult, List<String> lines) {
    if (value <= 0) return false;
    int maxShift = wordResult ? 31 : 63;
    if (isPowerOfTwo(value)) {
      int shift = Long.numberOfTrailingZeros(value);
      if (shift > maxShift) return false;
      lines.add("  " + shiftOpcode(wordResult) + " " + dst + ", " + lhs + ", " + shift);
      return true;
    }
    long lowBit = Long.lowestOneBit(value);
    long highBit = value - lowBit;
    boolean subtract = false;
    if (!isPowerOfTwo(highBit)) {
      highBit = value + lowBit;
      subtract = true;
    }
    int highShift = Long.numberOfTrailingZeros(highBit);
    if (!isPowerOfTwo(highBit) || highShift > maxShift) return false;
    emitSparseMultiply(
        lhs, dst, Long.numberOfTrailingZeros(lowBit), highShift, subtract, wordResult, lines);
    return true;
  }

  private void emitSparseMultiply(
      String lhs, String dst, int lowShift, int highShift, boolean subtract,
      boolean wordResult, List<String> lines) {
    String shift = shiftOpcode(wordResult);
    lines.add("  " + shift + " " + temporary + ", " + lhs + ", " + highShift);
    String low = lhs;
    if (lowShift != 0) {
      lines.add("  " + shift + " " + dst + ", " + lhs + ", " + lowShift);
      low = dst;
    }
    String opcode = (subtract ? "sub" : "add") + (wordResult ? "w" : "");
    String left = subtract ? temporary : low;
    String right = subtract ? low : temporary;
    lines.add("  " + opcode + " " + dst + ", " + left + ", " + right);
  }

  private static String shiftOpcode(boolean wordResult) { return wordResult ? "slliw" : "slli"; }
  private static boolean isPowerOfTwo(long value) { return value > 0 && (value & (value - 1)) == 0; }
}
