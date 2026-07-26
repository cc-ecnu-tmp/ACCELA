package accela.pass.ir.analysis.alias;

import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.BitSet;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Mutable read/write summary used while solving module-level ModRef information. */
final class MemoryEffects {
  final Set<GlobalVariable> reads = identitySet();
  final Set<GlobalVariable> writes = identitySet();
  final BitSet readArguments = new BitSet();
  final BitSet writtenArguments = new BitSet();
  boolean unknownRead;
  boolean unknownWrite;

  boolean addAccess(Value pointer, Function owner, boolean write) {
    Value root = PointerProvenance.root(pointer);
    if (root instanceof GlobalVariable global) {
      return (write ? writes : reads).add(global);
    }
    if (root instanceof Function.Argument argument && argument.getParent() == owner) {
      BitSet arguments = write ? writtenArguments : readArguments;
      boolean changed = !arguments.get(argument.getArgNo());
      arguments.set(argument.getArgNo());
      return changed;
    }
    if (root instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.ALLOCA) return false;
    return markUnknown(write);
  }

  boolean mergeCall(MemoryEffects callee, Instruction call, Function caller) {
    boolean changed = reads.addAll(callee.reads) | writes.addAll(callee.writes);
    changed |= markUnknown(false, callee.unknownRead);
    changed |= markUnknown(true, callee.unknownWrite);
    for (int index = callee.readArguments.nextSetBit(0);
        index >= 0; index = callee.readArguments.nextSetBit(index + 1)) {
      if (index < call.getNumOperands()) {
        changed |= addAccess(call.getOperand(index), caller, false);
      }
    }
    for (int index = callee.writtenArguments.nextSetBit(0);
        index >= 0; index = callee.writtenArguments.nextSetBit(index + 1)) {
      if (index < call.getNumOperands()) {
        changed |= addAccess(call.getOperand(index), caller, true);
      }
    }
    return changed;
  }

  boolean mayAccess(Instruction call, Value pointer, boolean write) {
    if (write ? unknownWrite : unknownRead) return true;
    Set<GlobalVariable> globals = write ? writes : reads;
    if (globals.stream().anyMatch(global -> PointerProvenance.mayAlias(global, pointer))) {
      return true;
    }
    BitSet arguments = write ? writtenArguments : readArguments;
    for (int index = arguments.nextSetBit(0);
        index >= 0; index = arguments.nextSetBit(index + 1)) {
      if (index < call.getNumOperands()
          && PointerProvenance.mayAlias(call.getOperand(index), pointer)) return true;
    }
    return false;
  }

  private boolean markUnknown(boolean write) {
    return markUnknown(write, true);
  }

  private boolean markUnknown(boolean write, boolean value) {
    if (!value || (write ? unknownWrite : unknownRead)) return false;
    if (write) unknownWrite = true;
    else unknownRead = true;
    return true;
  }

  private static Set<GlobalVariable> identitySet() {
    return Collections.newSetFromMap(new IdentityHashMap<>());
  }
}
