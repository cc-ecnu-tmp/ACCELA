package accela.pass.instrument;

import accela.pass.PassDescriptor;

/** Common identity carried by every typed optimization-remark event. */
public sealed interface OptimizationRemark permits PassRemark, DecisionRemark {
  String passId();

  int occurrence();

  PassDescriptor.Stage stage();

  String targetKind();

  String targetName();
}
