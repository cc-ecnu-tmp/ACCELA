#include <stdint.h>

#define UART ((volatile uint8_t *) 0x10000000UL)

static int at_line_start = 1;

static int uart_getc(void) {
  while ((UART[5] & 1) == 0) {}
  return UART[0];
}

static void uart_putc(int value) {
  while ((UART[5] & 0x20) == 0) {}
  UART[0] = (uint8_t) value;
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
  return negative ? (int) (0U - value) : (int) value;
}

int getarray(int values[]) {
  int count = getint();
  for (int index = 0; index < count; index++) values[index] = getint();
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
  *(volatile uint32_t *) 0x100000UL = 0x5555;
  for (;;) {}
}
