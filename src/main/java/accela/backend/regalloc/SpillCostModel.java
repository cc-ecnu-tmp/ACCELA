package accela.backend.regalloc;

import accela.backend.machine.VirtualRegister;

interface SpillCostModel {
  double cost(VirtualRegister register);
}
