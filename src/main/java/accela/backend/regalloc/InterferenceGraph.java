package accela.backend.regalloc;

import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

import accela.backend.machine.VirtualRegister;

public final class InterferenceGraph {
    private final Map<VirtualRegister, Set<VirtualRegister>> repr = new HashMap<>();
    
    public void addNode(VirtualRegister reg){
        repr.computeIfAbsent(reg, nbcs -> new HashSet<>());
    }

    public void addEdge(VirtualRegister a, VirtualRegister b){
        if (a.equals(b)){
            return;
        }

        addNode(a);
        addNode(b);

        repr.get(a).add(b);
        repr.get(b).add(a);
    }

    public boolean interferes(VirtualRegister a, VirtualRegister b){
        return repr.getOrDefault(a,Collections.emptySet()).contains(b);
    }

    public Set<VirtualRegister> neighbors(VirtualRegister register) {
      return Collections.unmodifiableSet(
          repr.getOrDefault(register, Collections.emptySet()));
    }

    public int degree(VirtualRegister register) {
      return repr.getOrDefault(register, Collections.emptySet()).size();
    }

    public Set<VirtualRegister> nodes() {
      return Collections.unmodifiableSet(repr.keySet());
    }

}
