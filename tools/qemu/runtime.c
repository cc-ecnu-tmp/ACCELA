#include <stddef.h>
#include <stdint.h>

#ifndef ACCELA_SPIKE
#define UART ((volatile uint8_t *) 0x10000000UL)
#else
volatile uint64_t tohost __attribute__((section(".tohost"), aligned(8)));
volatile uint64_t fromhost __attribute__((section(".tohost"), aligned(8)));
static volatile uintptr_t htif_args[8] __attribute__((aligned(64)));

static long htif_syscall(long number, long arg0, long arg1, long arg2) {
  htif_args[0] = (uintptr_t)number;
  htif_args[1] = (uintptr_t)arg0;
  htif_args[2] = (uintptr_t)arg1;
  htif_args[3] = (uintptr_t)arg2;
  __asm__ volatile("fence rw, rw" ::: "memory");
  tohost = (uintptr_t)htif_args;
  while (fromhost == 0) {}
  fromhost = 0;
  __asm__ volatile("fence rw, rw" ::: "memory");
  return (long)htif_args[0];
}
#endif

static int at_line_start = 1;
static int input_pushback = -1;

void *memset(void *destination, int value, size_t count) {
  unsigned char *bytes = destination;
  while (count-- != 0) *bytes++ = (unsigned char) value;
  return destination;
}

void *memcpy(void *destination, const void *source, size_t count) {
  unsigned char *output = destination;
  const unsigned char *input = source;
  while (count-- != 0) *output++ = *input++;
  return destination;
}

void *memmove(void *destination, const void *source, size_t count) {
  unsigned char *output = destination;
  const unsigned char *input = source;
  if ((uintptr_t) output <= (uintptr_t) input) return memcpy(destination, source, count);
  while (count-- != 0) output[count] = input[count];
  return destination;
}

static int uart_getc(void) {
  if (input_pushback >= 0) {
    int value = input_pushback;
    input_pushback = -1;
    return value;
  }
#ifdef ACCELA_SPIKE
  unsigned char value;
  return htif_syscall(63, 0, (long)&value, 1) == 1 ? value : -1;
#else
  while ((UART[5] & 1) == 0) {}
  return UART[0];
#endif
}

static void uart_putc(int value) {
#ifdef ACCELA_SPIKE
  unsigned char byte = (unsigned char)value;
  (void)htif_syscall(64, 1, (long)&byte, 1);
#else
  while ((UART[5] & 0x20) == 0) {}
  UART[0] = (uint8_t) value;
#endif
  at_line_start = value == '\n';
}

int getch(void) {
  return uart_getc();
}

int getint(void) {
  int ch;
  do {
    ch = uart_getc();
  } while (ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t');

  int negative = ch == '-';
  if (negative) ch = uart_getc();
  unsigned value = 0;
  while (ch >= '0' && ch <= '9') {
    value = value * 10 + (unsigned) (ch - '0');
    ch = uart_getc();
  }
  input_pushback = ch;
  return negative ? (int) (0U - value) : (int) value;
}

static int digit_value(int ch) {
  if (ch >= '0' && ch <= '9') return ch - '0';
  if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
  if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
  return -1;
}

static double scale_by_power(double value, double base, int exponent) {
  double factor = base;
  unsigned magnitude = exponent < 0 ? 0U - (unsigned) exponent : (unsigned) exponent;
  while (magnitude != 0) {
    if ((magnitude & 1) != 0) value = exponent < 0 ? value / factor : value * factor;
    factor *= factor;
    magnitude >>= 1;
  }
  return value;
}

float getfloat(void) {
  int ch;
  do {
    ch = uart_getc();
  } while (ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t');

  int negative = ch == '-';
  if (negative || ch == '+') ch = uart_getc();
  int base = 10;
  double value = 0;
  if (ch == '0') {
    ch = uart_getc();
    if (ch == 'x' || ch == 'X') {
      base = 16;
      ch = uart_getc();
    }
  }
  int digit;
  while ((digit = digit_value(ch)) >= 0 && digit < base) {
    value = value * base + digit;
    ch = uart_getc();
  }
  if (ch == '.') {
    double place = 1.0 / base;
    while ((digit = digit_value(ch = uart_getc())) >= 0 && digit < base) {
      value += digit * place;
      place /= base;
    }
  }
  if ((base == 10 && (ch == 'e' || ch == 'E'))
      || (base == 16 && (ch == 'p' || ch == 'P'))) {
    ch = uart_getc();
    int exponent_negative = ch == '-';
    if (exponent_negative || ch == '+') ch = uart_getc();
    int exponent = 0;
    while (ch >= '0' && ch <= '9') {
      if (exponent < 10000) {
        exponent = exponent * 10 + ch - '0';
        if (exponent > 10000) exponent = 10000;
      }
      ch = uart_getc();
    }
    value = scale_by_power(value, base == 10 ? 10.0 : 2.0,
                           exponent_negative ? -exponent : exponent);
  }
  input_pushback = ch;
  return negative ? -(float) value : (float) value;
}

int getarray(int values[]) {
  int count = getint();
  for (int index = 0; index < count; index++) values[index] = getint();
  return count;
}

int getfarray(float values[]) {
  int count = getint();
  for (int index = 0; index < count; index++) values[index] = getfloat();
  return count;
}

void putch(int value) {
  uart_putc(value);
}

void putint(int value) {
  unsigned magnitude = value < 0 ? 0U - (unsigned) value : (unsigned) value;
  if (value < 0) uart_putc('-');
  char digits[10];
  int count = 0;
  do {
    digits[count++] = (char) ('0' + magnitude % 10);
    magnitude /= 10;
  } while (magnitude != 0);
  while (count != 0) uart_putc(digits[--count]);
}

void putarray(int count, int values[]) {
  putint(count);
  uart_putc(':');
  for (int index = 0; index < count; index++) {
    uart_putc(' ');
    putint(values[index]);
  }
  uart_putc('\n');
}

static void put_string(const char *text) {
  while (*text != '\0') uart_putc(*text++);
}

void putfloat(float value) {
  union {
    float value;
    uint32_t bits;
  } raw = {value};
  if ((raw.bits >> 31) != 0) uart_putc('-');

  uint32_t exponent = (raw.bits >> 23) & 0xff;
  uint32_t mantissa = raw.bits & 0x7fffff;
  if (exponent == 0xff) {
    put_string(mantissa == 0 ? "inf" : "nan");
    return;
  }
  if (exponent == 0 && mantissa == 0) {
    put_string("0x0p+0");
    return;
  }

  int power;
  uint32_t fraction;
  if (exponent == 0) {
    int leading = 0;
    for (uint32_t scan = mantissa; scan > 1; scan >>= 1) leading++;
    power = leading - 149;
    fraction = (mantissa ^ (1U << leading)) << (24 - leading);
  } else {
    power = (int) exponent - 127;
    fraction = mantissa << 1;
  }

  put_string("0x1");
  if (fraction != 0) {
    static const char hex[] = "0123456789abcdef";
    int last = 0;
    while (last < 5 && ((fraction >> (last * 4)) & 0xf) == 0) last++;
    uart_putc('.');
    for (int digit = 5; digit >= last; digit--) {
      uart_putc(hex[(fraction >> (digit * 4)) & 0xf]);
    }
  }
  uart_putc('p');
  uart_putc(power < 0 ? '-' : '+');
  putint(power < 0 ? -power : power);
}

void putfarray(int count, float values[]) {
  putint(count);
  uart_putc(':');
  for (int index = 0; index < count; index++) {
    uart_putc(' ');
    putfloat(values[index]);
  }
  uart_putc('\n');
}

void _sysy_starttime(int line) {
  (void) line;
  __asm__ volatile("addi zero, zero, 291");
}

void _sysy_stoptime(int line) {
  (void) line;
  __asm__ volatile("addi zero, zero, 292");
}

__attribute__((noreturn)) void qemu_exit(int code) {
  if (!at_line_start) uart_putc('\n');
  putint(code & 255);
  uart_putc('\n');
#ifdef ACCELA_SPIKE
  (void)htif_syscall(93, 0, 0, 0);
#else
  *(volatile uint32_t *) 0x100000UL = 0x5555;
#endif
  for (;;) {}
}
