package accela.backend.machine;

public final class BlockOperand extends MachineOperand {
  private final MachineBasicBlock block;

  public BlockOperand(MachineBasicBlock block) {
    super(Kind.BLOCK);
    this.block = block;
  }

  public MachineBasicBlock getBlock() {
    return block;
  }
}
