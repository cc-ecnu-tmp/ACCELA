package accela.backend.regalloc;

import accela.backend.machine.VirtualRegister;

interface SpillCostModel {
  double cost(VirtualRegister register);

  default double cost(VirtualRegister register, int degree) {
    return cost(register);
  }

  default void combine(VirtualRegister representative, VirtualRegister merged) {}
}
