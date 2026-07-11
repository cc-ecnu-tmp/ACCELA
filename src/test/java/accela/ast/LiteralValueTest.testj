package accela.ast;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

final class LiteralValueTest {
  @Test
  void parsesIntegerRadicesAndSuffixes() {
    assertEquals(42, LiteralValue.parse("42").asInt());
    assertEquals(63, LiteralValue.parse("077").asInt());
    assertEquals(255, LiteralValue.parse("0xFF").asInt());
    assertEquals(42, LiteralValue.parse("42uL").asInt());
    assertEquals(-1, LiteralValue.parse("0xFFFFFFFF").asInt());
  }

  @Test
  void parsesDecimalAndHexadecimalFloats() {
    assertEquals(1.5f, LiteralValue.parse("1.5f").asFloat());
    assertEquals(8.0f, LiteralValue.parse("0x1.0p3").asFloat());
    assertTrue(LiteralValue.parse("1e2").isFloat());
    assertFalse(LiteralValue.parse("0x1f").isFloat());
  }

  @Test
  void preservesRuntimeNumberKind() {
    assertInstanceOf(Integer.class, LiteralValue.ofInt(3).asNumber());
    assertInstanceOf(Float.class, LiteralValue.ofFloat(3.0f).asNumber());
  }
}
