package accela.pass;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** R1 single source of truth; experimental transformations are deliberately absent. */
public final class PassRegistry {
  public static final String IR_SSA = "ir.ssa-construction";
  public static final String IR_LICM = "ir.licm";
  public static final String IR_UNROLL_1 = "ir.loop-unroll.1";
  public static final String IR_UNROLL_2 = "ir.loop-unroll.2";
  public static final String IR_STRENGTH = "ir.strength-reduction";
  public static final String IR_INLINER = "ir.inliner";
  public static final String LOWERING = "backend.ir-lowering";
  public static final String PHI_ELIMINATION = "backend.phi-elimination";
  public static final String GLOBAL_MERGE = "backend.global-merge";
  public static final String MACHINE_LICM = "backend.machine-licm";
  public static final String REGISTER_ALLOCATION = "backend.register-allocation";
  public static final String EMISSION = "backend.asm-emission";

  private static final PassRegistry R1 = new PassRegistry(List.of(
      required(IR_SSA, PassDescriptor.Stage.IR),
      optional(IR_LICM, PassDescriptor.Stage.IR, IR_SSA, "ir.licm.memory-and-dominance"),
      optional(IR_UNROLL_1, PassDescriptor.Stage.IR, IR_LICM, "ir.loop-unroll.1.constant-trip"),
      optional(IR_UNROLL_2, PassDescriptor.Stage.IR, IR_UNROLL_1, "ir.loop-unroll.2.constant-trip"),
      optional(IR_STRENGTH, PassDescriptor.Stage.IR, IR_UNROLL_2,
          "ir.strength-reduction.signed-arithmetic"),
      optional(IR_INLINER, PassDescriptor.Stage.IR, IR_STRENGTH,
          "ir.inliner.direct-call-and-return"),
      required(LOWERING, PassDescriptor.Stage.LOWERING, IR_INLINER),
      required(PHI_ELIMINATION, PassDescriptor.Stage.LOWERING, LOWERING),
      optional(GLOBAL_MERGE, PassDescriptor.Stage.MIR, PHI_ELIMINATION,
          "backend.global-merge.address-equivalence"),
      optional(MACHINE_LICM, PassDescriptor.Stage.MIR, GLOBAL_MERGE,
          "backend.machine-licm.loop-invariance"),
      required(REGISTER_ALLOCATION, PassDescriptor.Stage.REGISTER_ALLOCATION, MACHINE_LICM),
      required(EMISSION, PassDescriptor.Stage.EMISSION, REGISTER_ALLOCATION)));

  private final Map<String, PassDescriptor> descriptors;
  private final List<PassDescriptor> order;

  public PassRegistry(List<PassDescriptor> descriptors) {
    LinkedHashMap<String, PassDescriptor> byId = new LinkedHashMap<>();
    int previousStage = -1;
    for (PassDescriptor descriptor : descriptors) {
      if (byId.putIfAbsent(descriptor.id(), descriptor) != null) {
        throw new IllegalArgumentException("duplicate pass id: " + descriptor.id());
      }
      if (descriptor.stage().ordinal() < previousStage) {
        throw new IllegalArgumentException("pass stage order regresses at " + descriptor.id());
      }
      previousStage = descriptor.stage().ordinal();
      for (String dependency : descriptor.dependencies()) {
        if (!byId.containsKey(dependency)) {
          throw new IllegalArgumentException("pass dependency must precede its consumer: " + dependency);
        }
      }
    }
    this.descriptors = Map.copyOf(byId);
    order = List.copyOf(byId.values());
  }

  public static PassRegistry r1() { return R1; }

  public PassDescriptor require(String id) {
    PassDescriptor result = descriptors.get(id);
    if (result == null) throw new IllegalArgumentException("unknown pass id: " + id);
    return result;
  }

  public List<PassDescriptor> all() { return order; }

  public List<PassDescriptor> optional(PassDescriptor.Stage stage) {
    return order.stream().filter(pass -> !pass.required() && pass.stage() == stage).toList();
  }

  private static PassDescriptor required(String id, PassDescriptor.Stage stage, String... dependency) {
    return new PassDescriptor(id, stage, true, List.of(dependency), List.of());
  }

  private static PassDescriptor optional(String id, PassDescriptor.Stage stage, String dependency,
      String obligation) {
    return new PassDescriptor(id, stage, false, List.of(dependency), List.of(obligation));
  }
}
