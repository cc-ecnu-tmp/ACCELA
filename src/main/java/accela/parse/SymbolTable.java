package accela.parse;

import accela.ast.Node;
import java.util.HashMap;
import java.util.Map;

/** A simple nested symbol table mapping names to declaration Nodes. */
public class SymbolTable {
  private final Map<String, Node> symbols = new HashMap<>();
  private final SymbolTable parent;

  public SymbolTable(SymbolTable parent) {
    this.parent = parent;
  }

  public void put(String name, Node node) {
    symbols.put(name, node);
  }

  public Node get(String name) {
    Node n = symbols.get(name);
    if (n != null) return n;
    if (parent != null) return parent.get(name);
    return null;
  }

  public SymbolTable getParent() {
    return parent;
  }
}
