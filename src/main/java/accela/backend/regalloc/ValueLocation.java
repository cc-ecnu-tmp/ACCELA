package accela.backend.regalloc;

public interface ValueLocation {
  boolean isRegister();

  boolean isStack();
}
