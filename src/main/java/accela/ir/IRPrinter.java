package accela.ir;

import accela.ir.Instruction.Opcode;
import java.io.PrintStream;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Emits a structured IR {@link Module} as readable LLVM-like text.
 *
 * <p>This printer is primarily a debugging and inspection tool. It assigns stable textual names to
 * SSA results within each function and renders the subset of IR supported by this project in a form
 * that is intentionally close to LLVM IR, even though the in-memory IR is much simpler.
 */
public class IRPrinter {
  private final PrintStream out;
  private final Map<Value, String> valueNames = new HashMap<>();
  private final Map<Value, String> globalNames = new HashMap<>();
  private int regCounter;

  public IRPrinter(PrintStream out) {
    this.out = out;
  }

  /** Prints declarations, globals, and function definitions in module order. */
  public void print(Module module) {
    for (Function decl : module.getDeclares()) {
      printDeclare(decl);
    }
    if (!module.getDeclares().isEmpty()) out.println();

    for (GlobalVariable gv : module.getGlobals()) {
      printGlobal(gv);
    }
    if (!module.getGlobals().isEmpty()) out.println();

    for (Function func : module.getFunctions()) {
      printFunction(func);
    }
  }

  /** Prints a single external declaration. */
  private void printDeclare(Function func) {
    StringBuilder sb = new StringBuilder("declare ");
    sb.append(func.getReturnType()).append(" @").append(func.getName()).append("(");
    List<Function.Argument> args = func.getArguments();
    for (int i = 0; i < args.size(); i++) {
      if (i > 0) sb.append(", ");
      sb.append(args.get(i).getType());
    }
    sb.append(")");
    out.println(sb);
  }

  /** Prints a single module-level global definition. */
  private void printGlobal(GlobalVariable gv) {
    String kind = gv.isConstant() ? "constant" : "global";
    Type valType = gv.getValueType();
    String init = printConstant(gv.getInitializer());
    out.println("@" + gv.getName() + " = dso_local " + kind + " " + valType + " " + init);
    valueNames.put(gv, "@" + gv.getName());
    globalNames.put(gv, "@" + gv.getName());
  }

  /** Prints one function body, assigning textual names to arguments and SSA results first. */
  private void printFunction(Function func) {
    valueNames.clear();
    valueNames.putAll(globalNames);
    regCounter = 0;

    for (Function.Argument arg : func.getArguments()) {
      valueNames.put(arg, arg.getName());
    }

    for (BasicBlock bb : func.getBlocks()) {
      for (Instruction inst : bb.getInstructions()) {
        if (inst.hasResult()) {
          valueNames.put(inst, "%v" + (regCounter++));
        }
      }
    }

    StringBuilder header = new StringBuilder("define dso_local ");
    header.append(func.getReturnType()).append(" @").append(func.getName()).append("(");
    List<Function.Argument> args = func.getArguments();
    for (int i = 0; i < args.size(); i++) {
      if (i > 0) header.append(", ");
      header.append(args.get(i).getType()).append(" ").append(args.get(i).getName());
    }
    header.append(") {");
    out.println(header);

    for (BasicBlock bb : func.getBlocks()) {
      out.println(bb.getLabel() + ":");
      for (Instruction inst : bb.getInstructions()) {
        out.println("  " + printInstruction(inst));
      }
      out.println();
    }
    out.println("}");
    out.println();
  }

  private String printInstruction(Instruction inst) {
    switch (inst.getOpcode()) {
      case ADD:  return printBinOp(inst, "add");
      case SUB:  return printBinOp(inst, "sub");
      case MUL:  return printBinOp(inst, "mul");
      case SMULH: return printSMulH(inst);
      case SDIV: return printBinOp(inst, "sdiv");
      case SREM: return printBinOp(inst, "srem");
      case SHL:  return printBinOp(inst, "shl");
      case ASHR: return printBinOp(inst, "ashr");
      case AND:  return printBinOp(inst, "and");

      case FADD: return printBinOp(inst, "fadd");
      case FSUB: return printBinOp(inst, "fsub");
      case FMUL: return printBinOp(inst, "fmul");
      case FDIV: return printBinOp(inst, "fdiv");
      case FNEG: return printFNeg(inst);

      case ICMP: return printCmp(inst, "icmp");
      case FCMP: return printCmp(inst, "fcmp");

      case ALLOCA: return printAlloca(inst);
      case LOAD:   return printLoad(inst);
      case STORE:  return printStore(inst);
      case GEP:    return printGEP(inst);

      case BR:     return printBr(inst);
      case CONDBR: return printCondBr(inst);
      case RET:    return printRet(inst);

      case ZEXT:   return printConv(inst, "zext");
      case SEXT:   return printConv(inst, "sext");
      case SITOFP: return printConv(inst, "sitofp");
      case FPTOSI: return printConv(inst, "fptosi");

      case BUILD_VECTOR:    return printBuildVector(inst);
      case SPLAT:           return printSplat(inst);
      case EXTRACT_ELEMENT: return printExtractElement(inst);
      case INSERT_ELEMENT:  return printInsertElement(inst);
      case SHUFFLE_VECTOR:  return printShuffleVector(inst);

      case XOR:  return printBinOp(inst, "xor");
      case CALL: return printCall(inst);
      case PHI:  return printPhi(inst);
      case SELECT: return printSelect(inst);
      case VCIX: return printVCIX(inst);
      default:   return "; unknown instruction";
    }
  }

  // %v0 = add i32 %v1, %v2
  private String printBinOp(Instruction inst, String op) {
    String lhs = val(inst.getOperand(0));
    String rhs = val(inst.getOperand(1));
    return name(inst) + " = " + op + " " + inst.getType() + " " + lhs + ", " + rhs;
  }

  private String printSelect(Instruction inst) {
    return name(inst) + " = select i1 " + val(inst.getOperand(0)) + ", "
        + typed(inst.getOperand(1)) + ", " + typed(inst.getOperand(2));
  }

  private String printVCIX(Instruction inst) {
    String operands =
        java.util.stream.IntStream.range(0, inst.getNumOperands())
            .mapToObj(index -> typed(inst.getOperand(index)))
            .collect(java.util.stream.Collectors.joining(", "));
    String prefix = inst.hasResult() ? name(inst) + " = " : "";
    return prefix + "vcix \"" + inst.getVCIXInfo().mnemonic() + "\" " + operands;
  }

  /** Expands the internal i32 smulh primitive into valid LLVM IR for debug execution. */
  private String printSMulH(Instruction inst) {
    String result = name(inst);
    String left = result + ".lhs";
    String right = result + ".rhs";
    String product = result + ".product";
    String high = result + ".high";
    return left + " = sext i32 " + val(inst.getOperand(0)) + " to i64\n  "
        + right + " = sext i32 " + val(inst.getOperand(1)) + " to i64\n  "
        + product + " = mul i64 " + left + ", " + right + "\n  "
        + high + " = ashr i64 " + product + ", 32\n  "
        + result + " = trunc i64 " + high + " to i32";
  }

  // %v0 = fneg float %v1
  private String printFNeg(Instruction inst) {
    return name(inst) + " = fneg " + inst.getType() + " " + val(inst.getOperand(0));
  }

  // %v0 = icmp slt i32 %v1, %v2
  private String printCmp(Instruction inst, String kind) {
    Value lhs = inst.getOperand(0);
    return name(inst) + " = " + kind + " " + inst.getPredicate() + " "
        + lhs.getType() + " " + val(lhs) + ", " + val(inst.getOperand(1));
  }

  // %v0 = alloca i32
  private String printAlloca(Instruction inst) {
    return name(inst) + " = alloca " + inst.getAllocatedType();
  }

  // %v0 = load i32, ptr %v1
  private String printLoad(Instruction inst) {
    return name(inst) + " = load " + inst.getType() + ", ptr " + val(inst.getOperand(0));
  }

  // store i32 %v0, ptr %v1
  private String printStore(Instruction inst) {
    Value stored = inst.getOperand(0);
    return "store " + typed(stored) + ", ptr " + val(inst.getOperand(1));
  }

  // %v0 = getelementptr inbounds [5 x i32], ptr %v1, i64 0, i64 %v2
  private String printGEP(Instruction inst) {
    StringBuilder sb = new StringBuilder();
    sb.append(name(inst)).append(" = getelementptr ");
    if (inst.isGepInbounds()) sb.append("inbounds ");
    sb.append(inst.getGepSourceType()).append(", ptr ").append(val(inst.getOperand(0)));
    for (int i = 1; i < inst.getNumOperands(); i++) {
      Value idx = inst.getOperand(i);
      sb.append(", ").append(idx.getType()).append(" ").append(val(idx));
    }
    return sb.toString();
  }

  // br label %bb
  private String printBr(Instruction inst) {
    BasicBlock target = (BasicBlock) inst.getOperand(0);
    return "br label %" + target.getLabel();
  }

  // br i1 %v0, label %bb1, label %bb2
  private String printCondBr(Instruction inst) {
    BasicBlock ifTrue = (BasicBlock) inst.getOperand(1);
    BasicBlock ifFalse = (BasicBlock) inst.getOperand(2);
    return "br i1 " + val(inst.getOperand(0))
        + ", label %" + ifTrue.getLabel()
        + ", label %" + ifFalse.getLabel();
  }

  // ret i32 %v0  |  ret void
  private String printRet(Instruction inst) {
    if (inst.getNumOperands() == 0) return "ret void";
    Value retVal = inst.getOperand(0);
    return "ret " + typed(retVal);
  }

  // %v0 = zext i1 %v1 to i32
  private String printConv(Instruction inst, String op) {
    Value src = inst.getOperand(0);
    return name(inst) + " = " + op + " " + typed(src) + " to " + inst.getType();
  }

  // %v0 = build_vector <4 x i32> [i32 1, i32 2, i32 3, i32 4]
  private String printBuildVector(Instruction inst) {
    StringBuilder sb = new StringBuilder();
    sb.append(name(inst)).append(" = build_vector ").append(inst.getType()).append(" [");
    for (int index = 0; index < inst.getNumOperands(); index++) {
      if (index > 0) sb.append(", ");
      sb.append(typed(inst.getOperand(index)));
    }
    return sb.append("]").toString();
  }

  // %v0 = splat <4 x i32> i32 %v1
  private String printSplat(Instruction inst) {
    return name(inst) + " = splat " + inst.getType() + " " + typed(inst.getOperand(0));
  }

  // %v0 = extractelement <4 x i32> %v1, i32 2
  private String printExtractElement(Instruction inst) {
    return name(inst) + " = extractelement " + typed(inst.getOperand(0)) + ", "
        + typed(inst.getOperand(1));
  }

  // %v0 = insertelement <4 x i32> %v1, i32 %v2, i32 2
  private String printInsertElement(Instruction inst) {
    return name(inst) + " = insertelement " + typed(inst.getOperand(0)) + ", "
        + typed(inst.getOperand(1)) + ", " + typed(inst.getOperand(2));
  }

  // %v0 = shufflevector <4 x i32> %v1, <4 x i32> %v2, <4 x i32> <...>
  private String printShuffleVector(Instruction inst) {
    return name(inst) + " = shufflevector " + typed(inst.getOperand(0)) + ", "
        + typed(inst.getOperand(1)) + ", " + typed(inst.getOperand(2));
  }

  // %v0 = call i32 @foo(i32 %v1, float %v2)
  private String printCall(Instruction inst) {
    Function callee = inst.getCallee();
    StringBuilder sb = new StringBuilder();
    if (inst.hasResult()) {
      sb.append(name(inst)).append(" = ");
    }
    sb.append("call ").append(inst.getType()).append(" @").append(callee.getName()).append("(");
    for (int i = 0; i < inst.getNumOperands(); i++) {
      if (i > 0) sb.append(", ");
      sb.append(typed(inst.getOperand(i)));
    }
    sb.append(")");
    return sb.toString();
  }

  private String printPhi(Instruction inst) {
    // PHI nodes have pairs of (value, block) operands
    StringBuilder sb = new StringBuilder();
    sb.append(name(inst)).append(" = phi ").append(inst.getType()).append(" ");
    for (int i = 0; i < inst.getNumOperands(); i += 2) {
      if (i > 0) sb.append(", ");
      Value value = inst.getOperand(i);
      BasicBlock block = (BasicBlock) inst.getOperand(i + 1);
      sb.append("[ ").append(val(value)).append(", %").append(block.getLabel()).append(" ]");
    }
    return sb.toString();
  }

  /** Get the assigned name for a value (e.g., "%v0", "@foo", "42"). */
  private String val(Value v) {
    if (v instanceof Constant) return constVal((Constant) v);
    String n = valueNames.get(v);
    return n != null ? n : "<unnamed>";
  }

  /** Format "type name" (e.g., "i32 %v0", "float 0x..."). */
  private String typed(Value v) {
    return v.getType() + " " + val(v);
  }

  /** Get the register name for an instruction result. */
  private String name(Instruction inst) {
    return valueNames.getOrDefault(inst, "%?");
  }

  /** Print a constant value. */
  private String constVal(Constant c) {
    if (c instanceof Constant.Int) {
      Constant.Int ci = (Constant.Int) c;
      if (c.getType() == Type.I1) return ci.value != 0 ? "true" : "false";
      return String.valueOf(ci.value);
    }
    if (c instanceof Constant.Float) {
      return c.getName();
    }
    if (c instanceof Constant.Zero) {
      return "zeroinitializer";
    }
    if (c instanceof Constant.Array) {
      return printConstantArray((Constant.Array) c);
    }
    if (c instanceof Constant.Vector) {
      return printConstantVector((Constant.Vector) c);
    }
    return "0";
  }

  private String printConstant(Constant c) {
    return constVal(c);
  }

  private String printConstantArray(Constant.Array arr) {
    StringBuilder sb = new StringBuilder("[");
    Type elemType = arr.getType().innerType;
    for (int i = 0; i < arr.elements.size(); i++) {
      if (i > 0) sb.append(", ");
      Constant elem = arr.elements.get(i);
      sb.append(elemType).append(" ").append(constVal(elem));
    }
    sb.append("]");
    return sb.toString();
  }

  private String printConstantVector(Constant.Vector vector) {
    StringBuilder sb = new StringBuilder("<");
    for (int i = 0; i < vector.elements.size(); i++) {
      if (i > 0) sb.append(", ");
      Constant element = vector.elements.get(i);
      sb.append(element.getType()).append(" ").append(constVal(element));
    }
    sb.append(">");
    return sb.toString();
  }
}
