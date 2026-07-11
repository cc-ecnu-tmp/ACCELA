package accela.backend.lowering;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import org.junit.jupiter.api.Test;

final class GlobalAddressMaterializationTest {
  @Test
  void materializesOnlyFrequentlyUsedGlobalAddresses() {
    MachineFunction function = new MachineFunction("globals", MachineType.VOID);
    MachineBasicBlock entry = function.addBlock("entry");
    MachineBasicBlock body = function.addBlock("body");
    addUse(body, "hot");
    addUse(body, "hot");
    addUse(body, "hot");
    addUse(entry, "cold");
    addUse(body, "cold");
    MachineInstr branch = new MachineInstr(MachineOpcode.BR, null);
    branch.addOperand(new BlockOperand(body));
    entry.addInstruction(branch);
    body.addInstruction(new MachineInstr(MachineOpcode.RET, null));

    assertTrue(new GlobalAddressMaterialization().run(function));

    MachineInstr materialize = entry.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == MachineOpcode.MOVE)
        .findFirst().orElseThrow();
    assertEquals(MachineOpcode.MOVE, materialize.getOpcode());
    assertEquals("hot",
        assertInstanceOf(SymbolOperand.class, materialize.getOperands().getFirst()).getSymbol());
    assertTrue(function.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction != materialize)
        .flatMap(instruction -> instruction.getOperands().stream())
        .filter(VRegOperand.class::isInstance).count() == 3);
    assertEquals(2, function.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .flatMap(instruction -> instruction.getOperands().stream())
        .filter(SymbolOperand.class::isInstance)
        .map(SymbolOperand.class::cast)
        .filter(symbol -> symbol.getSymbol().equals("cold")).count());
  }

  private static void addUse(MachineBasicBlock block, String symbol) {
    MachineInstr instruction = new MachineInstr(MachineOpcode.LOAD, null);
    instruction.setType(MachineType.I32);
    instruction.addOperand(new SymbolOperand(symbol));
    block.addInstruction(instruction);
  }
}
