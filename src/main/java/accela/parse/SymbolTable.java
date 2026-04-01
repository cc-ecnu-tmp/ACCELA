package accela.parse;

import accela.ast.Node;
import java.util.HashMap;
import java.util.Map;

/**
 * Minimal nested symbol table used by Sema.
 *
 * <p>Each scope stores bindings from source names to declaration AST nodes. Lookup walks outward
 * through parent scopes, which is enough for this frontend because later stages keep the bound
 * declaration directly on each reference node.
 */
public class SymbolTable {
  private final Map<String, Node> symbols = new HashMap<>();
  private final SymbolTable parent;

  public SymbolTable(SymbolTable parent) {
    this.parent = parent;
  }

  /** Binds a name in the current lexical scope. */
  public void put(String name, Node node) {
    symbols.put(name, node);
  }

  /** Resolves a name by searching the current scope and then its ancestors. */
  public Node get(String name) {
    Node n = symbols.get(name);
    if (n != null) return n;
    if (parent != null) return parent.get(name);
    return null;
  }

  /** Returns the immediately enclosing scope. */
  public SymbolTable getParent() {
    return parent;
  }
}
