#include <stddef.h>
#include <stdint.h>

#define UART ((volatile uint8_t *) 0x10000000UL)
#define FINISHER ((volatile uint32_t *) 0x100000UL)
#define FINISHER_PASS 0x5555U
#define FINISHER_FAIL 0x3333U
#define FW_CFG_BASE 0x10100000UL
#define FW_CFG_DMA_REGISTER ((volatile uint64_t *) (FW_CFG_BASE + 0x10UL))
#define FW_CFG_FILE_DIRECTORY 0x0019U
#define FW_CFG_FILE_FIRST 0x0020U
#define FW_CFG_FILE_LAST 0x3fffU
#define FW_CFG_DMA_CONTROL_ERROR 0x01U
#define FW_CFG_DMA_CONTROL_READ 0x02U
#define FW_CFG_DMA_CONTROL_SELECT 0x08U
#define FW_CFG_DMA_SIGNATURE 0x51454d5520434647ULL
#define FW_CFG_DIRECTORY_ENTRY_SIZE 64U
#define FW_CFG_DIRECTORY_NAME_SIZE 56U
#define ACCELA_INPUT_CHUNK_SIZE 4096U
#define ACCELA_INPUT_DMA_SPIN_LIMIT 10000000U
#define ACCELA_ALWAYS_INLINE static inline __attribute__((always_inline))

static int at_line_start = 1;

struct accela_fw_cfg_dma_access {
  uint32_t control;
  uint32_t length;
  uint64_t address;
};

struct accela_input_transport {
  struct accela_fw_cfg_dma_access dma;
  uint8_t bytes[ACCELA_INPUT_CHUNK_SIZE];
};

_Static_assert(sizeof(struct accela_fw_cfg_dma_access) == 16,
               "fw_cfg DMA descriptor must be 16 bytes");
_Static_assert(sizeof(struct accela_input_transport) == 4112,
               "input transport layout must match the linker contract");

static volatile struct accela_input_transport __accela_input_transport
    __attribute__((aligned(16), section(".sysy_input_transport"), used));
static uint32_t __accela_input_size;
static uint32_t __accela_input_offset;
static uint32_t __accela_input_chunk_offset;
static uint32_t __accela_input_chunk_size;
static uint16_t __accela_input_selector;
static int __accela_input_initialized;
static int __accela_input_selected;
static int __accela_input_pushback_valid;
static int __accela_input_pushback;

__attribute__((noreturn)) void qemu_exit(int code);

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

ACCELA_ALWAYS_INLINE void uart_putc(int value) {
  while ((UART[5] & 0x20) == 0) {}
  UART[0] = (uint8_t) value;
  at_line_start = value == '\n';
}

ACCELA_ALWAYS_INLINE uint16_t __accela_input_be16(
    const volatile uint8_t *bytes) {
  return (uint16_t) ((uint16_t) bytes[0] << 8 | bytes[1]);
}

ACCELA_ALWAYS_INLINE uint32_t __accela_input_be32(
    const volatile uint8_t *bytes) {
  return (uint32_t) bytes[0] << 24
      | (uint32_t) bytes[1] << 16
      | (uint32_t) bytes[2] << 8
      | bytes[3];
}

__attribute__((noinline, optimize("O0"), visibility("hidden")))
uint32_t __accela_input_to_be32(uint32_t value) {
  return ((value & 0x000000ffU) << 24)
      | ((value & 0x0000ff00U) << 8)
      | ((value & 0x00ff0000U) >> 8)
      | ((value & 0xff000000U) >> 24);
}

__attribute__((noinline, optimize("O0"), visibility("hidden")))
uint64_t __accela_input_to_be64(uint64_t value) {
  uint64_t low = __accela_input_to_be32((uint32_t) value);
  uint64_t high = __accela_input_to_be32((uint32_t) (value >> 32));
  return low << 32 | high;
}

ACCELA_ALWAYS_INLINE void __accela_input_fence(void) {
  __asm__ volatile("fence iorw, iorw" ::: "memory");
}

__attribute__((noreturn, noinline, visibility("hidden")))
void __accela_input_fail(const char *reason) {
  static const char prefix[] = "ACCELA_INPUT_ERROR ";
  for (const char *cursor = prefix; *cursor != '\0'; cursor++) {
    uart_putc(*cursor);
  }
  while (*reason != '\0') uart_putc(*reason++);
  uart_putc('\n');
  *FINISHER = 125U << 16 | FINISHER_FAIL;
  for (;;) {}
}

__attribute__((noinline, visibility("hidden")))
void __accela_input_dma_read(uint16_t selector, int select,
                             volatile uint8_t *destination,
                             uint32_t length) {
  if (length == 0 || length > ACCELA_INPUT_CHUNK_SIZE) {
    __accela_input_fail("invalid_dma_length");
  }

  volatile struct accela_fw_cfg_dma_access *dma =
      &__accela_input_transport.dma;
  uint32_t control = FW_CFG_DMA_CONTROL_READ;
  if (select) {
    control |= FW_CFG_DMA_CONTROL_SELECT | (uint32_t) selector << 16;
  }
  dma->length = __accela_input_to_be32(length);
  dma->address = __accela_input_to_be64((uint64_t) (uintptr_t) destination);
  dma->control = __accela_input_to_be32(control);
  __accela_input_fence();
  *FW_CFG_DMA_REGISTER =
      __accela_input_to_be64((uint64_t) (uintptr_t) dma);
  __accela_input_fence();

  uint32_t result = control;
  for (uint32_t spin = 0; spin < ACCELA_INPUT_DMA_SPIN_LIMIT; spin++) {
    result = __accela_input_to_be32(dma->control);
    if (result == 0 || (result & FW_CFG_DMA_CONTROL_ERROR) != 0) break;
    __accela_input_fence();
  }
  if ((result & FW_CFG_DMA_CONTROL_ERROR) != 0) {
    __accela_input_fail("dma_error");
  }
  if (result != 0) __accela_input_fail("dma_timeout");
  __accela_input_fence();
}

ACCELA_ALWAYS_INLINE int __accela_input_name_terminated(
    const volatile uint8_t *name) {
  for (uint32_t index = 0; index < FW_CFG_DIRECTORY_NAME_SIZE; index++) {
    if (name[index] == 0) return 1;
  }
  return 0;
}

ACCELA_ALWAYS_INLINE int __accela_input_name_matches(
    const volatile uint8_t *name) {
  static const char expected[] = "opt/accela/sysy-input";
  uint32_t index = 0;
  while (expected[index] != '\0') {
    if (index >= FW_CFG_DIRECTORY_NAME_SIZE
        || name[index] != (uint8_t) expected[index]) {
      return 0;
    }
    index++;
  }
  return index < FW_CFG_DIRECTORY_NAME_SIZE && name[index] == 0;
}

__attribute__((noinline, visibility("hidden")))
void __accela_input_initialize(void) {
  if (__accela_input_initialized) return;

  uint64_t signature = __accela_input_to_be64(*FW_CFG_DMA_REGISTER);
  if (signature != FW_CFG_DMA_SIGNATURE) {
    __accela_input_fail("dma_unavailable");
  }

  volatile uint8_t *entry = __accela_input_transport.bytes;
  __accela_input_dma_read(FW_CFG_FILE_DIRECTORY, 1, entry, 4);
  uint32_t count = __accela_input_be32(entry);
  if (count == 0 || count > 0x4000U) {
    __accela_input_fail("invalid_directory_count");
  }

  int found = 0;
  for (uint32_t index = 0; index < count; index++) {
    __accela_input_dma_read(0, 0, entry, FW_CFG_DIRECTORY_ENTRY_SIZE);
    uint32_t size = __accela_input_be32(entry);
    uint16_t selector = __accela_input_be16(entry + 4);
    if (entry[6] != 0 || entry[7] != 0
        || selector < FW_CFG_FILE_FIRST || selector > FW_CFG_FILE_LAST
        || !__accela_input_name_terminated(entry + 8)) {
      __accela_input_fail("invalid_directory_entry");
    }
    if (__accela_input_name_matches(entry + 8)) {
      if (found) __accela_input_fail("duplicate_input_file");
      found = 1;
      __accela_input_selector = selector;
      __accela_input_size = size;
    }
  }
  if (!found) __accela_input_fail("missing_input_file");
  __accela_input_initialized = 1;
}

__attribute__((noinline, visibility("hidden")))
int __accela_input_getc(void) {
  if (__accela_input_pushback_valid) {
    __accela_input_pushback_valid = 0;
    return __accela_input_pushback;
  }
  __accela_input_initialize();
  if (__accela_input_offset == __accela_input_size) return -1;
  if (__accela_input_chunk_offset == __accela_input_chunk_size) {
    uint32_t remaining = __accela_input_size - __accela_input_offset;
    uint32_t length = remaining < ACCELA_INPUT_CHUNK_SIZE
        ? remaining : ACCELA_INPUT_CHUNK_SIZE;
    __accela_input_dma_read(__accela_input_selector,
                            !__accela_input_selected,
                            __accela_input_transport.bytes, length);
    __accela_input_selected = 1;
    __accela_input_chunk_offset = 0;
    __accela_input_chunk_size = length;
  }
  int value = __accela_input_transport.bytes[__accela_input_chunk_offset++];
  __accela_input_offset++;
  return value;
}

ACCELA_ALWAYS_INLINE void __accela_input_ungetc(int ch) {
  if (ch < 0) return;
  if (ch > 255 || __accela_input_pushback_valid) {
    __accela_input_fail("invalid_pushback");
  }
  __accela_input_pushback = ch;
  __accela_input_pushback_valid = 1;
}

int getch(void) {
  return __accela_input_getc();
}

int getint(void) {
  int ch;
  do {
    ch = __accela_input_getc();
  } while (ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t'
           || ch == '\f' || ch == '\v');
  if (ch < 0) __accela_input_fail("unexpected_eof_getint");

  int negative = ch == '-';
  if (negative || ch == '+') ch = __accela_input_getc();
  if (ch < '0' || ch > '9') __accela_input_fail("invalid_integer");
  unsigned value = 0;
  while (ch >= '0' && ch <= '9') {
    value = value * 10 + (unsigned) (ch - '0');
    ch = __accela_input_getc();
  }
  __accela_input_ungetc(ch);
  return negative ? (int) (0U - value) : (int) value;
}

ACCELA_ALWAYS_INLINE int digit_value(int ch) {
  if (ch >= '0' && ch <= '9') return ch - '0';
  if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
  if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
  return -1;
}

ACCELA_ALWAYS_INLINE double scale_by_power(
    double value, double base, int exponent) {
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
    ch = __accela_input_getc();
  } while (ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t'
           || ch == '\f' || ch == '\v');
  if (ch < 0) __accela_input_fail("unexpected_eof_getfloat");

  int negative = ch == '-';
  if (negative || ch == '+') ch = __accela_input_getc();
  int base = 10;
  double value = 0;
  int mantissa_digits = 0;
  if (ch == '0') {
    mantissa_digits = 1;
    ch = __accela_input_getc();
    if (ch == 'x' || ch == 'X') {
      base = 16;
      mantissa_digits = 0;
      ch = __accela_input_getc();
    }
  }
  int digit;
  while ((digit = digit_value(ch)) >= 0 && digit < base) {
    value = value * base + digit;
    mantissa_digits++;
    ch = __accela_input_getc();
  }
  if (ch == '.') {
    double place = 1.0 / base;
    while ((digit = digit_value(ch = __accela_input_getc())) >= 0 && digit < base) {
      value += digit * place;
      place /= base;
      mantissa_digits++;
    }
  }
  if (mantissa_digits == 0) __accela_input_fail("invalid_float");
  if ((base == 10 && (ch == 'e' || ch == 'E'))
      || (base == 16 && (ch == 'p' || ch == 'P'))) {
    ch = __accela_input_getc();
    int exponent_negative = ch == '-';
    if (exponent_negative || ch == '+') ch = __accela_input_getc();
    int exponent = 0;
    int exponent_digits = 0;
    while (ch >= '0' && ch <= '9') {
      exponent_digits++;
      if (exponent < 10000) {
        exponent = exponent * 10 + ch - '0';
        if (exponent > 10000) exponent = 10000;
      }
      ch = __accela_input_getc();
    }
    if (exponent_digits == 0) __accela_input_fail("invalid_float_exponent");
    value = scale_by_power(value, base == 10 ? 10.0 : 2.0,
                           exponent_negative ? -exponent : exponent);
  }
  __accela_input_ungetc(ch);
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

ACCELA_ALWAYS_INLINE void put_string(const char *text) {
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
  *FINISHER = FINISHER_PASS;
  for (;;) {}
}
