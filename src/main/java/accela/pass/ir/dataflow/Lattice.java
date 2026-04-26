package accela.pass.ir.dataflow;

public interface Lattice<T> {
  T bot();
  T join(T a, T b);
  boolean isEqual(T a, T b);
}
