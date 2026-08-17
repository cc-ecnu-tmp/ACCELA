package accela.pass.ir.verify;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import accela.backend.machine.MachineType;
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
      case ADD, SUB, MUL, SDIV, SREM, SHL, ASHR, AND, XOR:
        verifyBinary(function, inst, true, false);
        break;
      case SMULH:
        verifyBinary(function, inst, true, false);
        if (scalarElement(inst.getType()) != Type.INT) {
          fail(function, "SMULH requires i32 operands and result");
        }
        break;
      case FADD, FSUB, FMUL, FDIV:
        verifyBinary(function, inst, false, true);
        break;
      case FNEG:
        if (inst.getNumOperands() != 1
            || !inst.getType().equals(inst.getOperand(0).getType())
            || !isFloatScalarOrVector(inst.getType())) {
          fail(function, "FNEG requires equal float scalar/vector operand and result types");
        }
        break;
      case ICMP:
        verifyCompare(function, inst, true);
        break;
      case FCMP:
        verifyCompare(function, inst, false);
        break;
      case ZEXT, SEXT:
        verifyConversion(function, inst, true, true);
        break;
      case SITOFP:
        verifyConversion(function, inst, true, false);
        break;
      case FPTOSI:
        verifyConversion(function, inst, false, true);
        break;
      case BUILD_VECTOR:
        verifyBuildVector(function, inst);
        break;
      case SPLAT:
        verifySplat(function, inst);
        break;
      case EXTRACT_ELEMENT:
        verifyExtractElement(function, inst);
        break;
      case INSERT_ELEMENT:
        verifyInsertElement(function, inst);
        break;
      case SHUFFLE_VECTOR:
        verifyShuffleVector(function, inst);
        break;
      case SELECT:
        if (inst.getNumOperands() != 3) {
          fail(function, "SELECT requires exactly three operands");
        }
        Type conditionType = inst.getOperand(0).getType();
        boolean validCondition =
            conditionType == Type.I1
                || inst.getType().isVector()
                    && conditionType.isVector()
                    && conditionType.getElementType() == Type.I1
                    && conditionType.getLaneCount() == inst.getType().getLaneCount();
        if (!validCondition
            || !inst.getType().equals(inst.getOperand(1).getType())
            || !inst.getType().equals(inst.getOperand(2).getType())) {
          fail(function, "SELECT requires scalar/vector i1 and two equal-typed values");
        }
        break;
      case VCIX:
        verifyVCIX(function, inst);
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
          if (inst.getNumOperands() != 1
              || !inst.getOperand(0).getType().equals(function.getReturnType())) {
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
      if (!inst.getOperand(i).getType().equals(inst.getType())) {
        fail(function, "phi incoming value type mismatch");
      }
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

  private static void verifyVCIX(Function function, Instruction instruction) {
    var info = instruction.getVCIXInfo();
    if (info == null) fail(function, "VCIX instruction requires encoding metadata");
    if (info.writesVectorDestination() != instruction.getType().isVector()) {
      fail(function, "VCIX result type disagrees with destination encoding");
    }
    int expected =
        1 + (info.form().hasVectorSource2() ? 1 : 0) + (info.form().readsDestination() ? 1 : 0);
    if (instruction.getNumOperands() != expected) {
      fail(function, "VCIX operand count does not match " + info.form());
    }
    int index = 0;
    if (info.form().readsDestination()) {
      if (!instruction.getOperand(index).getType().isVector()) {
        fail(function, "three-source VCIX old destination must be a vector");
      }
      if (info.writesVectorDestination()
          && !instruction.getOperand(index).getType().equals(instruction.getType())) {
        fail(function, "three-source VCIX old destination must match its result");
      }
      index++;
    }
    if (info.form().hasVectorSource2()) {
      if (!instruction.getOperand(index).getType().isVector()) {
        fail(function, "VCIX vs2 operand must be a vector");
      }
      index++;
    }
    Value argument = instruction.getOperand(index);
    if (info.form().hasVectorSource1() && !argument.getType().isVector()) {
      fail(function, "VCIX vs1 operand must be a vector");
    }
    if (info.form().hasIntegerScalar()
        && (argument.getType().isVector() || !argument.getType().isInteger())) {
      fail(function, "VCIX integer operand must be a scalar integer");
    }
    if (info.form().hasFloatScalar()
        && (argument.getType().isVector() || !argument.getType().isFloat())) {
      fail(function, "VCIX floating operand must be a scalar float");
    }
    if (info.form().hasImmediate()
        && (!(argument instanceof accela.ir.Constant.Int immediate)
            || immediate.value < -16
            || immediate.value > 15)) {
      fail(function, "VCIX immediate must be a signed five-bit integer constant");
    }
    if (info.form().isWidening()) {
      Type wideType =
          info.writesVectorDestination() ? instruction.getType() : instruction.getOperand(0).getType();
      int vs2Index = info.form().readsDestination() ? 1 : 0;
      Type narrowType = instruction.getOperand(vs2Index).getType();
      if (!wideType.isVector()
          || !narrowType.isVector()
          || wideType.getLaneCount() != narrowType.getLaneCount()
          || MachineType.fromIr(wideType.getElementType()).getSize()
              != 2 * MachineType.fromIr(narrowType.getElementType()).getSize()) {
        fail(function, "widening VCIX requires a double-width vd and matching narrow vs2");
      }
      if (info.form().hasVectorSource1()
          && !argument.getType().equals(narrowType)) {
        fail(function, "widening VCIX vs1 must match the narrow vs2 type");
      }
    }
    boolean hasVectorState =
        instruction.getType().isVector()
            || java.util.stream.IntStream.range(0, instruction.getNumOperands())
                .anyMatch(operand -> instruction.getOperand(operand).getType().isVector());
    if (!hasVectorState && instruction.getVCIXConfig() == null) {
      fail(function, "VCIX without vector values requires explicit vl/vtype state");
    }
  }

  private static void verifyBinary(
      Function function, Instruction inst, boolean integer, boolean floating) {
    if (inst.getNumOperands() != 2
        || !inst.getOperand(0).getType().equals(inst.getType())
        || !inst.getOperand(1).getType().equals(inst.getType())) {
      fail(function, inst.getOpcode() + " requires equal operand and result types");
    }
    if (integer && !isIntegerScalarOrVector(inst.getType())) {
      fail(function, inst.getOpcode() + " requires integer scalar/vector operands");
    }
    if (floating && !isFloatScalarOrVector(inst.getType())) {
      fail(function, inst.getOpcode() + " requires float scalar/vector operands");
    }
  }

  private static void verifyCompare(Function function, Instruction inst, boolean integer) {
    if (inst.getNumOperands() != 2
        || !inst.getOperand(0).getType().equals(inst.getOperand(1).getType())
        || inst.getPredicate() == null) {
      fail(function, inst.getOpcode() + " requires equal operands and a predicate");
    }
    Type operandType = inst.getOperand(0).getType();
    if (integer
        ? (!operandType.isPointer() && !isIntegerScalarOrVector(operandType))
        : !isFloatScalarOrVector(operandType)) {
      fail(function, inst.getOpcode() + " operand category mismatch");
    }
    Type expected = operandType.isVector()
        ? Type.vector(Type.I1, operandType.getLaneCount()) : Type.I1;
    if (!inst.getType().equals(expected)) {
      fail(function, inst.getOpcode() + " result must be i1 or an equal-width i1 vector");
    }
  }

  private static void verifyConversion(
      Function function, Instruction inst, boolean sourceInteger, boolean destinationInteger) {
    if (inst.getNumOperands() != 1) fail(function, "invalid " + inst.getOpcode());
    Type source = inst.getOperand(0).getType();
    Type destination = inst.getType();
    if (!source.hasSameShape(destination)) {
      fail(function, inst.getOpcode() + " must preserve scalar/vector shape and lane count");
    }
    if (sourceInteger ? !isIntegerScalarOrVector(source) : !isFloatScalarOrVector(source)) {
      fail(function, inst.getOpcode() + " source type mismatch");
    }
    if (destinationInteger
        ? !isIntegerScalarOrVector(destination) : !isFloatScalarOrVector(destination)) {
      fail(function, inst.getOpcode() + " destination type mismatch");
    }
  }

  private static void verifyBuildVector(Function function, Instruction inst) {
    if (!inst.getType().isVector() || inst.getNumOperands() != inst.getType().getLaneCount()) {
      fail(function, "BUILD_VECTOR requires exactly one operand per result lane");
    }
    for (int i = 0; i < inst.getNumOperands(); i++) {
      if (!inst.getOperand(i).getType().equals(inst.getType().getElementType())) {
        fail(function, "BUILD_VECTOR operand type must equal the vector element type");
      }
    }
  }

  private static void verifySplat(Function function, Instruction inst) {
    if (!inst.getType().isVector()
        || inst.getNumOperands() != 1
        || !inst.getOperand(0).getType().equals(inst.getType().getElementType())) {
      fail(function, "SPLAT requires one operand of the vector element type");
    }
  }

  private static void verifyExtractElement(Function function, Instruction inst) {
    if (inst.getNumOperands() != 2
        || !inst.getOperand(0).getType().isVector()
        || !isIntegerLike(inst.getOperand(1).getType())
        || !inst.getType().equals(inst.getOperand(0).getType().getElementType())) {
      fail(function, "invalid EXTRACT_ELEMENT");
    }
  }

  private static void verifyInsertElement(Function function, Instruction inst) {
    if (inst.getNumOperands() != 3
        || !inst.getType().isVector()
        || !inst.getOperand(0).getType().equals(inst.getType())
        || !inst.getOperand(1).getType().equals(inst.getType().getElementType())
        || !isIntegerLike(inst.getOperand(2).getType())) {
      fail(function, "invalid INSERT_ELEMENT");
    }
  }

  private static void verifyShuffleVector(Function function, Instruction inst) {
    if (inst.getNumOperands() != 3
        || !inst.getType().isVector()
        || !inst.getOperand(0).getType().isVector()
        || !inst.getOperand(0).getType().equals(inst.getOperand(1).getType())
        || !inst.getType().getElementType().equals(inst.getOperand(0).getType().getElementType())
        || !(inst.getOperand(2) instanceof accela.ir.Constant.Vector mask)
        || mask.getType().getElementType() != Type.INT
        || mask.getType().getLaneCount() != inst.getType().getLaneCount()) {
      fail(function, "SHUFFLE_VECTOR requires equal input vectors and a matching i32 mask");
    }
  }

  private static boolean isIntegerScalarOrVector(Type type) {
    return scalarElement(type).isInteger();
  }

  private static boolean isFloatScalarOrVector(Type type) {
    return scalarElement(type).isFloat();
  }

  private static Type scalarElement(Type type) {
    return type.isVector() ? type.getElementType() : type;
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
