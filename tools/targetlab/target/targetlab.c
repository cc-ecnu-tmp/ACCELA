#include <stddef.h>
#include <stdint.h>

#ifndef TARGETLAB_BAREMETAL
#include <setjmp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#endif

#define WARMUPS 2
#define SAMPLES 9
#define TARGET_CYCLES 1000000ULL
#define MAILBOX_MAGIC 0x414343454c414d42ULL
#define MAX_BENCHMARKS 180

typedef uint64_t (*kernel_fn)(uint64_t, uint64_t *);
struct targetlab_descriptor { const char *metric; const char *category; kernel_fn kernel; };
extern const struct targetlab_descriptor targetlab_descriptors[];
extern const size_t targetlab_descriptor_count;
extern uint64_t targetlab_empty(uint64_t, uint64_t *);

struct mailbox_entry {
  char metric[48];
  char category[16];
  char source[24];
  uint64_t sample_count;
  uint64_t values[SAMPLES];
} __attribute__((packed));

struct mailbox {
  uint64_t magic;
  uint32_t version;
  uint32_t status;
  uint64_t count;
  struct mailbox_entry entries[MAX_BENCHMARKS];
} __attribute__((packed));

volatile struct mailbox targetlab_mailbox __attribute__((section(".targetlab.mailbox"), used));
static uint64_t memory_words[1024] __attribute__((aligned(64)));

#ifndef TARGETLAB_BAREMETAL
static sigjmp_buf counter_probe;
static volatile sig_atomic_t probing_counter;
static int cycle_available;

static void illegal_instruction(int signal_number) {
  (void)signal_number;
  if (probing_counter) siglongjmp(counter_probe, 1);
  _Exit(125);
}
#endif

static inline uint64_t read_cycle(void) {
  uint64_t value;
  __asm__ volatile("rdcycle %0" : "=r"(value));
  return value;
}

#ifndef TARGETLAB_BAREMETAL
static uint64_t read_clock_ns(void) {
  struct timespec value;
  if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) _Exit(124);
  return (uint64_t)value.tv_sec * 1000000000ULL + (uint64_t)value.tv_nsec;
}

static void probe_timer(void) {
  struct sigaction action = {0};
  action.sa_handler = illegal_instruction;
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGILL, &action, NULL) != 0) _Exit(123);
  probing_counter = 1;
  if (sigsetjmp(counter_probe, 1) == 0) {
    (void)read_cycle();
    cycle_available = 1;
  } else {
    cycle_available = 0;
  }
  probing_counter = 0;
}
#endif

static uint64_t timer_now(void) {
#ifdef TARGETLAB_BAREMETAL
  return read_cycle();
#else
  return cycle_available ? read_cycle() : read_clock_ns();
#endif
}

static const char *timer_source(void) {
#ifdef TARGETLAB_BAREMETAL
  return "rdcycle_x1000";
#else
  return cycle_available ? "rdcycle_x1000" : "clock_gettime_ns_x1000";
#endif
}

static void copy_text(volatile char *destination, size_t capacity, const char *source) {
  size_t index = 0;
  while (index + 1 < capacity && source[index]) {
    destination[index] = source[index];
    index++;
  }
  destination[index] = 0;
}

static uint64_t elapsed(kernel_fn kernel, uint64_t iterations) {
  uint64_t start = timer_now();
  volatile uint64_t checksum = kernel(iterations, memory_words);
  uint64_t end = timer_now();
  (void)checksum;
  return end > start ? end - start : 0;
}

static uint64_t choose_iterations(kernel_fn kernel) {
  uint64_t iterations = 1024;
  while (iterations <= (1ULL << 30)) {
    if (elapsed(kernel, iterations) >= TARGET_CYCLES) return iterations;
    iterations *= 2;
  }
  return 0;
}

static int measure(const struct targetlab_descriptor *descriptor) {
  uint64_t iterations = choose_iterations(descriptor->kernel);
  if (iterations == 0 || targetlab_mailbox.count >= MAX_BENCHMARKS) return 0;
  for (int warmup = 0; warmup < WARMUPS; warmup++) (void)elapsed(descriptor->kernel, iterations);
  volatile struct mailbox_entry *entry = &targetlab_mailbox.entries[targetlab_mailbox.count];
  copy_text(entry->metric, sizeof(entry->metric), descriptor->metric);
  copy_text(entry->category, sizeof(entry->category), descriptor->category);
  copy_text(entry->source, sizeof(entry->source), timer_source());
  entry->sample_count = SAMPLES;
  for (int sample = 0; sample < SAMPLES; sample++) {
    uint64_t baseline = elapsed(targetlab_empty, iterations);
    uint64_t measured = elapsed(descriptor->kernel, iterations);
    if (baseline == 0 || measured <= baseline) return 0;
    entry->values[sample] = (measured - baseline) * 1000ULL / iterations;
  }
  targetlab_mailbox.count++;
#ifndef TARGETLAB_BAREMETAL
  printf("{\"metric\":\"%s\",\"category\":\"%s\",\"source\":\"%s\",\"values\":[",
      descriptor->metric, descriptor->category, timer_source());
  for (int sample = 0; sample < SAMPLES; sample++) {
    if (sample) putchar(',');
    printf("%llu", (unsigned long long)entry->values[sample]);
  }
  puts("]}");
#endif
  return 1;
}

void targetlab_done(void) {
  __asm__ volatile("" ::: "memory");
#ifdef TARGETLAB_BAREMETAL
  for (;;) __asm__ volatile("wfi");
#endif
}

int main(void) {
#ifndef TARGETLAB_BAREMETAL
  probe_timer();
#endif
  targetlab_mailbox.magic = MAILBOX_MAGIC;
  targetlab_mailbox.version = 1;
  targetlab_mailbox.status = 0;
  targetlab_mailbox.count = 0;
  for (size_t index = 0; index < 1024; index++) memory_words[index] = index + 1;
  if (targetlab_descriptor_count > MAX_BENCHMARKS) return 2;
  for (size_t index = 0; index < targetlab_descriptor_count; index++) {
    if (!measure(&targetlab_descriptors[index])) return 3;
  }
  targetlab_mailbox.status = 1;
  targetlab_done();
  return 0;
}
