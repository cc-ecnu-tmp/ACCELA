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
#ifndef TARGETLAB_CLOCK_HZ
#error TARGETLAB_CLOCK_HZ must be defined
#endif
#ifndef TARGETLAB_MINIMUM_CYCLES
#error TARGETLAB_MINIMUM_CYCLES must be defined
#endif
#define MAILBOX_MAGIC 0x414343454c414d42ULL
#define MAX_BENCHMARKS 256

typedef uint64_t (*kernel_fn)(uint64_t, uint64_t *);
struct targetlab_descriptor {
  const char *metric;
  const char *category;
  kernel_fn kernel;
  kernel_fn baseline;
  uint64_t normalization;
};
extern const struct targetlab_descriptor targetlab_descriptors[];
extern const size_t targetlab_descriptor_count;
extern uint64_t targetlab_empty(uint64_t, uint64_t *);

struct mailbox_entry {
  char metric[48];
  char category[32];
  char source[24];
  uint64_t sample_count;
  uint64_t iterations;
  uint64_t normalization;
  uint64_t baseline_values[SAMPLES];
  uint64_t measured_values[SAMPLES];
  uint64_t values[SAMPLES];
} __attribute__((packed));

struct mailbox {
  uint64_t magic;
  uint32_t version;
  uint32_t status;
  uint64_t total_length;
  uint64_t count;
  uint32_t counter_flags;
  uint32_t reserved;
  uint64_t clock_hz;
  uint64_t minimum_cycles;
  uint32_t warmup_count;
  uint32_t sample_count;
  uint32_t measurement_mode;
  uint32_t reserved2;
  uint64_t failure_sample;
  uint64_t failure_baseline;
  uint64_t failure_measured;
  struct mailbox_entry entries[MAX_BENCHMARKS];
} __attribute__((packed));

volatile struct mailbox targetlab_mailbox __attribute__((section(".targetlab.mailbox"), used));
static uint64_t memory_words[32768] __attribute__((aligned(64)));
static int cycle_available;
static int instret_available;

#ifndef TARGETLAB_BAREMETAL
static sigjmp_buf counter_probe;
static volatile sig_atomic_t probing_counter;

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

static inline uint64_t read_instret(void) {
  uint64_t value;
  __asm__ volatile("rdinstret %0" : "=r"(value));
  return value;
}

#ifndef TARGETLAB_BAREMETAL
static uint64_t read_clock_ns(void) {
  struct timespec value;
  if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) _Exit(124);
  return (uint64_t)value.tv_sec * 1000000000ULL + (uint64_t)value.tv_nsec;
}

static int probe_counter(uint64_t (*reader)(void)) {
  probing_counter = 1;
  if (sigsetjmp(counter_probe, 1) == 0) {
    (void)reader();
    probing_counter = 0;
    return 1;
  }
  probing_counter = 0;
  return 0;
}

static void probe_timer(void) {
  struct sigaction action = {0};
  action.sa_handler = illegal_instruction;
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGILL, &action, NULL) != 0) _Exit(123);
  cycle_available = probe_counter(read_cycle);
  instret_available = probe_counter(read_instret);
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
  uint64_t threshold;
#ifdef TARGETLAB_BAREMETAL
  threshold = TARGETLAB_MINIMUM_CYCLES;
#else
  threshold = cycle_available ? TARGETLAB_MINIMUM_CYCLES
      : (TARGETLAB_MINIMUM_CYCLES * 1000000000ULL + TARGETLAB_CLOCK_HZ - 1) / TARGETLAB_CLOCK_HZ;
#endif
  while (iterations <= (1ULL << 30)) {
    if (elapsed(kernel, iterations) >= threshold) return iterations;
    iterations *= 2;
  }
  return 0;
}

static int measurement_failure(const struct targetlab_descriptor *descriptor, uint64_t sample,
                               uint64_t baseline, uint64_t measured, const char *reason) {
  targetlab_mailbox.failure_sample = sample;
  targetlab_mailbox.failure_baseline = baseline;
  targetlab_mailbox.failure_measured = measured;
#ifndef TARGETLAB_BAREMETAL
  fprintf(stderr, "TargetLab metric %s failed at sample %llu: %s "
      "(baseline=%llu measured=%llu)\n", descriptor->metric,
      (unsigned long long)sample, reason, (unsigned long long)baseline,
      (unsigned long long)measured);
#else
  (void)descriptor;
  (void)reason;
#endif
  return 0;
}

static int measure(const struct targetlab_descriptor *descriptor) {
  uint64_t iterations = choose_iterations(descriptor->kernel);
  if (iterations == 0 || targetlab_mailbox.count >= MAX_BENCHMARKS) {
    return measurement_failure(descriptor, UINT64_MAX, 0, 0, "iteration scaling failed");
  }
  for (int warmup = 0; warmup < WARMUPS; warmup++) {
    (void)elapsed(descriptor->baseline, iterations);
    (void)elapsed(descriptor->kernel, iterations);
  }
  volatile struct mailbox_entry *entry = &targetlab_mailbox.entries[targetlab_mailbox.count];
  copy_text(entry->metric, sizeof(entry->metric), descriptor->metric);
  copy_text(entry->category, sizeof(entry->category), descriptor->category);
  copy_text(entry->source, sizeof(entry->source), timer_source());
  entry->sample_count = SAMPLES;
  entry->iterations = iterations;
  entry->normalization = descriptor->normalization;
  if (descriptor->normalization == 0 || iterations > UINT64_MAX / descriptor->normalization) {
    return measurement_failure(descriptor, UINT64_MAX, 0, 0, "normalization overflow");
  }
  uint64_t denominator = iterations * descriptor->normalization;
  for (int sample = 0; sample < SAMPLES; sample++) {
    uint64_t baseline = elapsed(descriptor->baseline, iterations);
    uint64_t measured = elapsed(descriptor->kernel, iterations);
    if (baseline == 0 || measured <= baseline) {
      return measurement_failure(descriptor, (uint64_t)sample, baseline, measured,
          "baseline subtraction is non-positive");
    }
    if (measured - baseline > UINT64_MAX / 1000ULL) {
      return measurement_failure(descriptor, (uint64_t)sample, baseline, measured,
          "normalization overflow");
    }
    entry->baseline_values[sample] = baseline;
    entry->measured_values[sample] = measured;
    entry->values[sample] = (measured - baseline) * 1000ULL / denominator;
  }
  targetlab_mailbox.count++;
#ifndef TARGETLAB_BAREMETAL
  printf("{\"kind\":\"sample\",\"metric\":\"%s\",\"category\":\"%s\",\"source\":\"%s\",\"iterations\":%llu,\"normalization\":%llu,\"baseline_values\":[",
      descriptor->metric, descriptor->category, timer_source(),
      (unsigned long long)iterations, (unsigned long long)descriptor->normalization);
  for (int sample = 0; sample < SAMPLES; sample++) {
    if (sample) putchar(',');
    printf("%llu", (unsigned long long)entry->baseline_values[sample]);
  }
  printf("],\"measured_values\":[");
  for (int sample = 0; sample < SAMPLES; sample++) {
    if (sample) putchar(',');
    printf("%llu", (unsigned long long)entry->measured_values[sample]);
  }
  printf("],\"values\":[");
  for (int sample = 0; sample < SAMPLES; sample++) {
    if (sample) putchar(',');
    printf("%llu", (unsigned long long)entry->values[sample]);
  }
  puts("]}");
  fflush(stdout);
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
#else
  (void)read_cycle();
  (void)read_instret();
  cycle_available = 1;
  instret_available = 1;
#endif
  targetlab_mailbox.magic = MAILBOX_MAGIC;
  targetlab_mailbox.version = 1;
  targetlab_mailbox.status = 0;
  targetlab_mailbox.total_length = 0;
  targetlab_mailbox.count = 0;
  targetlab_mailbox.counter_flags = (cycle_available ? 1U : 0U) | (instret_available ? 2U : 0U);
  targetlab_mailbox.reserved = 0;
  targetlab_mailbox.clock_hz = TARGETLAB_CLOCK_HZ;
  targetlab_mailbox.minimum_cycles = TARGETLAB_MINIMUM_CYCLES;
  targetlab_mailbox.warmup_count = WARMUPS;
  targetlab_mailbox.sample_count = SAMPLES;
#ifdef TARGETLAB_QEMU_PROXY
  targetlab_mailbox.measurement_mode = 1;
#else
  targetlab_mailbox.measurement_mode = 0;
#endif
  targetlab_mailbox.reserved2 = 0;
  targetlab_mailbox.failure_sample = UINT64_MAX;
  targetlab_mailbox.failure_baseline = 0;
  targetlab_mailbox.failure_measured = 0;
#ifndef TARGETLAB_BAREMETAL
  printf("{\"kind\":\"environment\",\"backend\":\"linux\",\"rdcycle\":%s,\"rdinstret\":%s,\"timer\":\"%s\",\"clock_hz\":%llu,\"minimum_cycles\":%llu,\"warmup_count\":%u,\"sample_count\":%u,\"measurement_mode\":\"%s\"}\n",
      cycle_available ? "true" : "false", instret_available ? "true" : "false",
      cycle_available ? "rdcycle" : "clock_gettime",
      (unsigned long long)TARGETLAB_CLOCK_HZ, (unsigned long long)TARGETLAB_MINIMUM_CYCLES,
      WARMUPS, SAMPLES,
#ifdef TARGETLAB_QEMU_PROXY
      "qemu_proxy"
#else
      "hardware"
#endif
      );
  fflush(stdout);
#endif
  if (targetlab_descriptor_count > MAX_BENCHMARKS) {
    targetlab_mailbox.status = 2;
    targetlab_mailbox.total_length = sizeof(struct mailbox) - MAX_BENCHMARKS * sizeof(struct mailbox_entry);
    targetlab_done();
    return 2;
  }
  for (size_t index = 0; index < targetlab_descriptor_count; index++) {
    for (size_t memory_index = 0; memory_index < 32768; memory_index++) {
      memory_words[memory_index] =
          (uint64_t)(uintptr_t)&memory_words[(memory_index + 1) % 32768];
    }
#ifndef TARGETLAB_BAREMETAL
    fprintf(stderr, "TargetLab measuring %s\n", targetlab_descriptors[index].metric);
#endif
    if (!measure(&targetlab_descriptors[index])) {
      targetlab_mailbox.status = 3;
      targetlab_mailbox.total_length = sizeof(struct mailbox) - MAX_BENCHMARKS * sizeof(struct mailbox_entry)
          + targetlab_mailbox.count * sizeof(struct mailbox_entry);
      targetlab_done();
      return 3;
    }
  }
  targetlab_mailbox.total_length = sizeof(struct mailbox) - MAX_BENCHMARKS * sizeof(struct mailbox_entry)
      + targetlab_mailbox.count * sizeof(struct mailbox_entry);
  targetlab_mailbox.status = 1;
  targetlab_done();
  return 0;
}
