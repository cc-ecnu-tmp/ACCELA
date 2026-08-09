package accela.pass.instrument;

import accela.pass.PassDescriptor;

/** Destination for typed optimization-remark events. */
@FunctionalInterface
public interface PassRemarkSink {
  void accept(OptimizationRemark remark);

  default PassDecisionEmitter decisionEmitter(
      PassDescriptor descriptor, int occurrence, String targetKind, String targetName) {
    return new PassDecisionEmitter(this, descriptor, occurrence, targetKind, targetName);
  }

  static PassRemarkSink noop() {
    return ignored -> {};
  }
}
