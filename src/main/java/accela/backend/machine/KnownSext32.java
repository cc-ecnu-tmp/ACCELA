package accela.backend.machine;

/** Machine-IR fact for a value whose upper 32 bits are the sign extension of its low word. */
public record KnownSext32(VirtualRegister register) {
  public KnownSext32 {
    if (register == null) throw new IllegalArgumentException("register must be present");
  }
}
