package accela.util;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Small dependency-free JSON codec with strict duplicate-key and syntax validation. */
public final class StrictJson {
  private static final int MAX_DEPTH = 64;

  private StrictJson() {}

  public static Object parse(String json) {
    if (json == null) throw new IllegalArgumentException("JSON text must not be null");
    Parser parser = new Parser(json);
    Object value = parser.parseValue(0);
    parser.skipWhitespace();
    if (!parser.atEnd()) parser.fail("trailing content");
    return value;
  }

  public static String stringify(Object value) {
    StringBuilder output = new StringBuilder();
    writeValue(value, output, 0);
    return output.toString();
  }

  private static void writeValue(Object value, StringBuilder output, int depth) {
    if (depth > MAX_DEPTH) throw new IllegalArgumentException("JSON nesting exceeds " + MAX_DEPTH);
    if (value == null) {
      output.append("null");
    } else if (value instanceof String text) {
      writeString(text, output);
    } else if (value instanceof Boolean || value instanceof Byte || value instanceof Short
        || value instanceof Integer || value instanceof Long || value instanceof BigDecimal) {
      output.append(value);
    } else if (value instanceof Float number) {
      if (!Float.isFinite(number)) throw new IllegalArgumentException("JSON numbers must be finite");
      output.append(number);
    } else if (value instanceof Double number) {
      if (!Double.isFinite(number)) throw new IllegalArgumentException("JSON numbers must be finite");
      output.append(number);
    } else if (value instanceof Map<?, ?> map) {
      output.append('{');
      boolean first = true;
      for (Map.Entry<?, ?> entry : map.entrySet()) {
        if (!(entry.getKey() instanceof String key)) {
          throw new IllegalArgumentException("JSON object keys must be strings");
        }
        if (!first) output.append(',');
        first = false;
        writeString(key, output);
        output.append(':');
        writeValue(entry.getValue(), output, depth + 1);
      }
      output.append('}');
    } else if (value instanceof Iterable<?> iterable) {
      output.append('[');
      boolean first = true;
      for (Object element : iterable) {
        if (!first) output.append(',');
        first = false;
        writeValue(element, output, depth + 1);
      }
      output.append(']');
    } else {
      throw new IllegalArgumentException("unsupported JSON value type: " + value.getClass().getName());
    }
  }

  private static void writeString(String text, StringBuilder output) {
    output.append('"');
    for (int index = 0; index < text.length(); index++) {
      char ch = text.charAt(index);
      switch (ch) {
        case '"' -> output.append("\\\"");
        case '\\' -> output.append("\\\\");
        case '\b' -> output.append("\\b");
        case '\f' -> output.append("\\f");
        case '\n' -> output.append("\\n");
        case '\r' -> output.append("\\r");
        case '\t' -> output.append("\\t");
        default -> {
          if (ch < 0x20) output.append(String.format("\\u%04x", (int) ch));
          else output.append(ch);
        }
      }
    }
    output.append('"');
  }

  private static final class Parser {
    private final String input;
    private int index;

    Parser(String input) {
      this.input = input;
    }

    Object parseValue(int depth) {
      if (depth > MAX_DEPTH) fail("nesting exceeds " + MAX_DEPTH);
      skipWhitespace();
      if (atEnd()) fail("expected a value");
      return switch (input.charAt(index)) {
        case '{' -> parseObject(depth + 1);
        case '[' -> parseArray(depth + 1);
        case '"' -> parseString();
        case 't' -> parseLiteral("true", Boolean.TRUE);
        case 'f' -> parseLiteral("false", Boolean.FALSE);
        case 'n' -> parseLiteral("null", null);
        default -> parseNumber();
      };
    }

    private Map<String, Object> parseObject(int depth) {
      index++;
      LinkedHashMap<String, Object> object = new LinkedHashMap<>();
      skipWhitespace();
      if (consume('}')) return object;
      while (true) {
        skipWhitespace();
        if (atEnd() || input.charAt(index) != '"') fail("expected an object key");
        String key = parseString();
        skipWhitespace();
        if (!consume(':')) fail("expected ':' after object key");
        Object value = parseValue(depth);
        if (object.containsKey(key)) fail("duplicate object key '" + key + "'");
        object.put(key, value);
        skipWhitespace();
        if (consume('}')) return object;
        if (!consume(',')) fail("expected ',' or '}'");
      }
    }

    private List<Object> parseArray(int depth) {
      index++;
      List<Object> array = new ArrayList<>();
      skipWhitespace();
      if (consume(']')) return array;
      while (true) {
        array.add(parseValue(depth));
        skipWhitespace();
        if (consume(']')) return array;
        if (!consume(',')) fail("expected ',' or ']'");
      }
    }

    private String parseString() {
      index++;
      StringBuilder value = new StringBuilder();
      while (!atEnd()) {
        char ch = input.charAt(index++);
        if (ch == '"') return value.toString();
        if (ch < 0x20) fail("unescaped control character in string");
        if (ch != '\\') {
          value.append(ch);
          continue;
        }
        if (atEnd()) fail("unterminated escape sequence");
        char escaped = input.charAt(index++);
        switch (escaped) {
          case '"', '\\', '/' -> value.append(escaped);
          case 'b' -> value.append('\b');
          case 'f' -> value.append('\f');
          case 'n' -> value.append('\n');
          case 'r' -> value.append('\r');
          case 't' -> value.append('\t');
          case 'u' -> appendUnicodeEscape(value);
          default -> fail("unknown escape sequence '\\" + escaped + "'");
        }
      }
      fail("unterminated string");
      return null;
    }

    private void appendUnicodeEscape(StringBuilder value) {
      char first = parseHexCodeUnit();
      if (Character.isHighSurrogate(first)) {
        if (index + 1 >= input.length() || input.charAt(index) != '\\'
            || input.charAt(index + 1) != 'u') fail("high surrogate must be followed by a low surrogate");
        index += 2;
        char second = parseHexCodeUnit();
        if (!Character.isLowSurrogate(second)) fail("invalid low surrogate");
        value.append(first).append(second);
      } else if (Character.isLowSurrogate(first)) {
        fail("unexpected low surrogate");
      } else {
        value.append(first);
      }
    }

    private char parseHexCodeUnit() {
      if (index + 4 > input.length()) fail("incomplete unicode escape");
      int code = 0;
      for (int count = 0; count < 4; count++) {
        int digit = Character.digit(input.charAt(index++), 16);
        if (digit < 0) fail("invalid unicode escape");
        code = code * 16 + digit;
      }
      return (char) code;
    }

    private Object parseLiteral(String literal, Object value) {
      if (!input.startsWith(literal, index)) fail("invalid literal");
      index += literal.length();
      return value;
    }

    private BigDecimal parseNumber() {
      int start = index;
      consume('-');
      if (consume('0')) {
        if (!atEnd() && Character.isDigit(input.charAt(index))) fail("leading zero in number");
      } else {
        if (atEnd() || input.charAt(index) < '1' || input.charAt(index) > '9') fail("invalid number");
        while (!atEnd() && Character.isDigit(input.charAt(index))) index++;
      }
      if (consume('.')) {
        if (atEnd() || !Character.isDigit(input.charAt(index))) fail("missing fractional digits");
        while (!atEnd() && Character.isDigit(input.charAt(index))) index++;
      }
      if (!atEnd() && (input.charAt(index) == 'e' || input.charAt(index) == 'E')) {
        index++;
        if (!atEnd() && (input.charAt(index) == '+' || input.charAt(index) == '-')) index++;
        if (atEnd() || !Character.isDigit(input.charAt(index))) fail("missing exponent digits");
        while (!atEnd() && Character.isDigit(input.charAt(index))) index++;
      }
      try {
        return new BigDecimal(input.substring(start, index));
      } catch (NumberFormatException exception) {
        fail("invalid number");
        return null;
      }
    }

    void skipWhitespace() {
      while (!atEnd()) {
        char ch = input.charAt(index);
        if (ch != ' ' && ch != '\n' && ch != '\r' && ch != '\t') return;
        index++;
      }
    }

    boolean consume(char expected) {
      if (!atEnd() && input.charAt(index) == expected) {
        index++;
        return true;
      }
      return false;
    }

    boolean atEnd() {
      return index == input.length();
    }

    void fail(String message) {
      throw new IllegalArgumentException("invalid JSON at offset " + index + ": " + message);
    }
  }
}
