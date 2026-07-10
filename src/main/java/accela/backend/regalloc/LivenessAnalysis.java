package accela.backend.regalloc;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

public final class LivenessAnalysis {
  public static final class Result {
    private final Map<MachineBasicBlock,Set<VirtualRegister>> liveIn = new IdentityHashMap<>();
    private final Map<MachineBasicBlock,Set<VirtualRegister>> liveOut = new IdentityHashMap<>();
    private final Map<MachineInstr,Set<VirtualRegister>> liveBefore = new IdentityHashMap<>();
    private final Map<MachineInstr,Set<VirtualRegister>> liveAfter = new IdentityHashMap<>();

    public Set<VirtualRegister> liveIn(MachineBasicBlock block) {
      return liveIn.getOrDefault(block, Collections.emptySet());
    }

    public Set<VirtualRegister> liveOut(MachineBasicBlock block) {
      return liveOut.getOrDefault(block, Collections.emptySet());
    }

    public Set<VirtualRegister> liveBefore(MachineInstr instr) {
      return liveBefore.getOrDefault(instr, Collections.emptySet());
    }

    public Set<VirtualRegister> liveAfter(MachineInstr instr) {
      return liveAfter.getOrDefault(instr, Collections.emptySet());
    }
  }

  public static Result analyze (MachineFunction it) {
    Map<MachineBasicBlock, BlockUseDef> useDef = new IdentityHashMap<>();
    Map<MachineBasicBlock, List<MachineBasicBlock>> successors = new IdentityHashMap<>();

    it.getBlocks().forEach(block ->
        {
          useDef.put(block, getBlockUseDef(block));
          successors.put(block, getSuccessors(block));
        }
      );
    
    //liveOut[B] = union liveIn[S] for S in successors[B]
    //liveIn[B]  = use[B] union (liveOut[B] - def[B])  
    
    Result res = new Result();
    
    it.getBlocks().forEach(block -> {
      res.liveIn.put(block, new HashSet<>());
      res.liveOut.put(block, new HashSet<>());
    });
    
    var revBlocks = it.getBlocks().reversed();

    boolean changed = false;
    
    do{  
      changed = false;

      for(var blc : revBlocks){
        Set<VirtualRegister> newOut = new HashSet<>();
        successors.get(blc).forEach( s -> newOut.addAll( res.liveIn.getOrDefault(s, Collections.emptySet())) );
        
        var blcUseDef = useDef.get(blc);
        
        Set<VirtualRegister> newIn = new HashSet<>(newOut);
        newIn.removeAll(blcUseDef.def());
        newIn.addAll(blcUseDef.use());
        if (!newIn.equals(res.liveIn.get(blc))
              || !newOut.equals(res.liveOut.get(blc))) {
            res.liveIn.put(blc, newIn);
            res.liveOut.put(blc, newOut);
            changed = true;
          }
      }
    
    }while(changed);
    
    computeInstrLiveness(it, res);
    
    return res;

  }
  
  //liveAfter(last) = liveOut(block)
  //liveBefore(instr) = use(instr) ∪ (liveAfter(instr) - def(instr))
  //liveAfter(previous) = liveBefore(next)
  
  private static void computeInstrLiveness(MachineFunction function, Result result) {
    for (MachineBasicBlock block : function.getBlocks()) {
      Set<VirtualRegister> live = new HashSet<>(result.liveOut.get(block));
      List<MachineInstr> instrs = block.getInstructions();

      for (int i = instrs.size() - 1; i >= 0; i--) {
        MachineInstr instr = instrs.get(i);

        result.liveAfter.put(instr, new HashSet<>(live));

        if (instr.getDest() != null) {
          live.remove(instr.getDest());
        }

        live.addAll(getInstrUse(instr));

        result.liveBefore.put(instr, new HashSet<>(live));
      }
    }
  }

  private static List<MachineBasicBlock> getBlockTargets(MachineInstr instr) {
    List<MachineBasicBlock> result = new ArrayList<>();

    for (MachineOperand operand : instr.getOperands()) {
      if (operand instanceof BlockOperand) {
        MachineBasicBlock target = ((BlockOperand) operand).getBlock();
        if (!result.contains(target)) {
          result.add(target);
        }
      }
    }

    return result;
  }

  private static List<MachineBasicBlock> getSuccessors(MachineBasicBlock block) {
    List<MachineInstr> instrs = block.getInstructions();

    if (instrs.isEmpty()) {
      throw new IllegalStateException("empty machine block: " + block.getLabel());
    }

    MachineInstr last = instrs.getLast();

    if (last.getOpcode() == MachineOpcode.BR
        || last.getOpcode() == MachineOpcode.CONDBR) {
      return getBlockTargets(last);
    }

    if (last.getOpcode() == MachineOpcode.RET
        || last.getOpcode() == MachineOpcode.TAILCALL) {
      return Collections.emptyList();
    }

    throw new IllegalStateException(
        "unterminated machine block: " + block.getLabel());
  }

  private static Set<VirtualRegister> getInstrUse(MachineInstr it){
    return it.getOperands().stream()
        .filter(VRegOperand.class::isInstance)
        .map(VRegOperand.class::cast)
        .map(VRegOperand::getRegister)
        .collect(Collectors.toCollection(HashSet::new));
  }

  private record BlockUseDef(Set<VirtualRegister> use, Set<VirtualRegister> def) {
  }

  private static BlockUseDef getBlockUseDef(MachineBasicBlock block) {
    Set<VirtualRegister> use = new HashSet<>();
    Set<VirtualRegister> def = new HashSet<>();

    block.getInstructions().forEach(instr -> {
      getInstrUse(instr).stream()
          .filter(reg -> !def.contains(reg))
          .forEach(use::add);

      if (instr.getDest() != null) {
        def.add(instr.getDest());
      }
    });

    return new BlockUseDef(use, def);
  }

}
