package accela.backend.target;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineType;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

final class RISCVCalleeSavedFrameTest {
  @Test
  void preservesOnlyUsedCalleeSavedRegisters() {
    RISCVTarget target = new RISCVTarget();
    RISCVFrameLowering frame = new RISCVFrameLowering(target);
    MachineFunction function = new MachineFunction("callee_saved", MachineType.I32);
    function.getFrameInfo().markCalleeSavedRegister("s1");
    frame.finalizeFrame(function);
    List<String> lines = new ArrayList<>();

    frame.emitPrologue(function, lines);
    frame.emitEpilogue(function, lines);

    assertTrue(lines.stream().anyMatch(line -> line.matches("\\s+sd s1, \\d+\\(sp\\)")));
    assertTrue(lines.stream().anyMatch(line -> line.matches("\\s+ld s1, \\d+\\(sp\\)")));
    assertFalse(lines.stream().anyMatch(line -> line.contains("s2")));
  }

  @Test
  void preservesFloatingRegistersAtAbiWidth() {
    RISCVTarget target = new RISCVTarget();
    RISCVFrameLowering frame = new RISCVFrameLowering(target);
    MachineFunction function = new MachineFunction("float_callee_saved", MachineType.F32);
    function.getFrameInfo().markFloatCalleeSavedRegister("fs2");
    frame.finalizeFrame(function);
    List<String> lines = new ArrayList<>();

    frame.emitPrologue(function, lines);
    frame.emitEpilogue(function, lines);

    assertTrue(lines.stream().anyMatch(line -> line.matches("\\s+fsd fs2, \\d+\\(sp\\)")));
    assertTrue(lines.stream().anyMatch(line -> line.matches("\\s+fld fs2, \\d+\\(sp\\)")));
    assertFalse(lines.stream().anyMatch(line -> line.contains("fsw fs2")));
    assertFalse(lines.stream().anyMatch(line -> line.contains("flw fs2")));
  }
}
