package accela.backend.machine;

public final class SymbolOperand extends MachineOperand {
  private final String symbol;

  public SymbolOperand(String symbol) {
    super(Kind.SYMBOL);
    this.symbol = symbol;
  }

  public String getSymbol() {
    return symbol;
  }
}
