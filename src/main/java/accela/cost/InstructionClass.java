package accela.cost;

import accela.backend.machine.MachineOpcode;

/** Stable micro-architectural classes shared by TargetLab and the compiler. */
public enum InstructionClass {
  INTEGER_ALU,
  INTEGER_MUL,
  INTEGER_DIV,
  FLOAT_ALU,
  FLOAT_MUL,
  FLOAT_DIV,
  LOAD,
  STORE,
  BRANCH,
  CALL_RETURN,
  ADDRESS,
  MOVE;

  public static InstructionClass of(MachineOpcode opcode) {
    return switch (opcode) {
      case MUL, SMULH -> INTEGER_MUL;
      case DIV, REM -> INTEGER_DIV;
      case FADD, FSUB, FNEG -> FLOAT_ALU;
      case FMUL -> FLOAT_MUL;
      case FDIV -> FLOAT_DIV;
      case LOAD -> LOAD;
      case STORE, MEMZERO -> STORE;
      case BR, CONDBR -> BRANCH;
      case CALL, RET -> CALL_RETURN;
      case STACK_ADDR -> ADDRESS;
      case MOVE, ARG_IN, PHI -> MOVE;
      default -> INTEGER_ALU;
    };
  }
}
