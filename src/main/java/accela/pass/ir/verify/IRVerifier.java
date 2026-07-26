package accela.pass.ir.verify;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Structural verifier for the ACCELA IR. */
public final class IRVerifier {
  private IRVerifier() {}

  public static void verifyModule(accela.ir.Module module) {
    for (Function function : module.getFunctions()) {
      verifyFunction(function);
    }
  }

  public static void verifyFunction(Function function) {
    Set<String> labels = new HashSet<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!labels.add(block.getLabel())) {
        fail(function, "duplicate basic block label: " + block.getLabel());
      }
      verifyBlock(function, block);
    }
  }

  private static void verifyBlock(Function function, BasicBlock block) {
    if (block.getParent() != function) {
      fail(function, "basic block has wrong parent: " + block.getLabel());
    }

    List<Instruction> instructions = block.getInstructions();
    boolean seenNonPhi = false;
    for (int i = 0; i < instructions.size(); i++) {
      Instruction inst = instructions.get(i);
      if (inst.getParent() != block) {
        fail(function, "instruction has wrong parent in block " + block.getLabel());
      }
      if (inst.getOpcode() == Instruction.Opcode.PHI) {
        if (seenNonPhi) {
          fail(function, "phi must appear before non-phi instructions in block " + block.getLabel());
        }
      } else {
        seenNonPhi = true;
      }
      if (inst.isTerminator() && i != instructions.size() - 1) {
        fail(function, "terminator must be the last instruction in block " + block.getLabel());
      }
      verifyInstruction(function, block, inst);
    }

    if (!instructions.isEmpty() && !instructions.get(instructions.size() - 1).isTerminator()) {
      fail(function, "unterminated basic block: " + block.getLabel());
    }
  }

  private static void verifyInstruction(Function function, BasicBlock block, Instruction inst) {
    for (int i = 0; i < inst.getNumOperands(); i++) {
      Value operand = inst.getOperand(i);
      if (operand == null) {
        fail(function, "null operand in " + inst.getOpcode() + " at block " + block.getLabel());
      }
      if (!hasMatchingUse(operand, inst, i)) {
        fail(function, "broken use-list for operand " + i + " of " + inst.getOpcode());
      }
      if (operand instanceof Instruction operandInst && operandInst.getParent() == null) {
        fail(function, "instruction operand has been detached from its parent block");
      }
      if (operand instanceof Instruction operandInst
          && operandInst.getParent() != null
          && operandInst.getParent().getParent() != function) {
        fail(function, "instruction operand belongs to a block outside the current function");
      }
    }

    switch (inst.getOpcode()) {
      case SMULH, SHL, ASHR:
        if (inst.getType() != Type.INT
            || inst.getNumOperands() != 2
            || inst.getOperand(0).getType() != Type.INT
            || inst.getOperand(1).getType() != Type.INT) {
          fail(function, inst.getOpcode() + " requires i32 operands and result");
        }
        break;
      case ALLOCA:
        if (inst.getType() != Type.PTR || inst.getAllocatedType() == null || inst.getNumOperands() != 0) {
          fail(function, "invalid alloca");
        }
        break;
      case LOAD:
        if (inst.getNumOperands() != 1 || !inst.getOperand(0).getType().isPointer()) {
          fail(function, "invalid load");
        }
        break;
      case STORE:
        if (inst.getNumOperands() != 2 || !inst.getOperand(1).getType().isPointer()) {
          fail(function, "invalid store");
        }
        break;
      case GEP:
        if (inst.getNumOperands() < 2
            || !inst.getOperand(0).getType().isPointer()
            || inst.getType() != Type.PTR
            || inst.getGepSourceType() == null) {
          fail(function, "invalid gep");
        }
        for (int i = 1; i < inst.getNumOperands(); i++) {
          if (!isIntegerLike(inst.getOperand(i).getType())) {
            fail(function, "gep index must be integer-like");
          }
        }
        break;
      case BR:
        if (inst.getNumOperands() != 1 || !(inst.getOperand(0) instanceof BasicBlock target)
            || target.getParent() != function) {
          fail(function, "invalid br");
        }
        break;
      case CONDBR:
        if (inst.getNumOperands() != 3
            || inst.getOperand(0).getType() != Type.I1
            || !(inst.getOperand(1) instanceof BasicBlock trueTarget)
            || !(inst.getOperand(2) instanceof BasicBlock falseTarget)
            || trueTarget.getParent() != function
            || falseTarget.getParent() != function) {
          fail(function, "invalid condbr");
        }
        break;
      case RET:
        if (function.getReturnType() == Type.VOID) {
          if (inst.getNumOperands() != 0) fail(function, "void function must return void");
        } else {
          if (inst.getNumOperands() != 1 || inst.getOperand(0).getType() != function.getReturnType()) {
            fail(function, "return type mismatch");
          }
        }
        break;
      case PHI:
        verifyPhi(function, block, inst);
        break;
      default:
        break;
    }
  }

  private static void verifyPhi(Function function, BasicBlock block, Instruction inst) {
    if (inst.getNumOperands() == 0 || (inst.getNumOperands() & 1) != 0) {
      fail(function, "phi must contain value/block pairs");
    }
    List<BasicBlock> preds = collectPrintedPredecessors(function, block);
    Set<BasicBlock> incomingBlocks = new LinkedHashSet<>();
    for (int i = 0; i < inst.getNumOperands(); i += 2) {
      Value incomingBlockValue = inst.getOperand(i + 1);
      if (!(incomingBlockValue instanceof BasicBlock)) {
        fail(function, "phi incoming block must belong to the same function");
      }
      BasicBlock incomingBlock = (BasicBlock) incomingBlockValue;
      if (incomingBlock.getParent() != function) {
        fail(function, "phi incoming block must belong to the same function");
      }
      if (!preds.contains(incomingBlock)) {
        fail(function, "phi incoming block is not a predecessor of " + block.getLabel());
      }
      if (!incomingBlocks.add(incomingBlock)) {
        fail(function, "phi has duplicate incoming block " + incomingBlock.getLabel());
      }
    }
    if (incomingBlocks.size() != preds.size()) {
      fail(function, "phi must have exactly one incoming value for each predecessor of " + block.getLabel());
    }
  }

  private static boolean hasMatchingUse(Value value, Instruction user, int operandIndex) {
    for (Use use : value.getUses()) {
      if (use.getUser() == user && use.getOperandIndex() == operandIndex) return true;
    }
    return false;
  }

  private static boolean isIntegerLike(Type type) {
    return type == Type.I1 || type == Type.INT || type == Type.I64;
  }

  private static List<BasicBlock> collectPrintedPredecessors(Function function, BasicBlock block) {
    List<BasicBlock> preds = new ArrayList<>();
    for (BasicBlock candidate : function.getBlocks()) {
      for (BasicBlock succ : candidate.getSuccessors()) {
        if (succ == block || succ.getLabel().equals(block.getLabel())) {
          preds.add(candidate);
          break;
        }
      }
    }
    return preds;
  }

  private static void fail(Function function, String message) {
    throw new IllegalStateException("IR verification failed for @" + function.getName() + ": " + message);
  }
}
