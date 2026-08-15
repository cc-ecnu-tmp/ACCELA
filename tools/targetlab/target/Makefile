PYTHON ?= python3
TARGETLAB_BUILD_DIR ?= build/targetlab
TARGETLAB_CC ?= riscv64-linux-gnu-gcc
TARGETLAB_OBJCOPY ?= riscv64-linux-gnu-objcopy
TARGETLAB_BACKEND ?= linux
TARGETLAB_CLOCK_HZ ?= 50000000
TARGETLAB_MINIMUM_CYCLES ?= 1000000
TARGETLAB_MEASUREMENT_MODE ?= hardware

GENERATED_ASM := $(TARGETLAB_BUILD_DIR)/benchmarks.S
GENERATED_REGISTRY := $(TARGETLAB_BUILD_DIR)/registry.c
SOURCES := tools/targetlab/target/targetlab.c $(GENERATED_ASM) $(GENERATED_REGISTRY)

.PHONY: all clean
all: $(TARGETLAB_BUILD_DIR)/targetlab.elf

$(TARGETLAB_BUILD_DIR):
	mkdir -p $@

$(GENERATED_ASM): tools/targetlab/target/generate_asm.py | $(TARGETLAB_BUILD_DIR)
	$(PYTHON) $< $@

$(GENERATED_REGISTRY): tools/targetlab/target/generate_registry.py tools/targetlab/target/generate_asm.py | $(TARGETLAB_BUILD_DIR)
	$(PYTHON) $< $@ $(if $(filter qemu_proxy,$(TARGETLAB_MEASUREMENT_MODE)),--qemu-proxy,)

ifeq ($(TARGETLAB_BACKEND),baremetal)
ifeq ($(strip $(TARGETLAB_STARTUP)),)
$(error TARGETLAB_STARTUP is required for baremetal builds)
endif
ifeq ($(strip $(TARGETLAB_LINKER)),)
$(error TARGETLAB_LINKER is required for baremetal builds)
endif
TARGET_FLAGS := -DTARGETLAB_BAREMETAL -ffreestanding -fno-builtin -nostdlib \
	-march=rv64gc -mabi=lp64d -mcmodel=medany -Wl,-T,$(TARGETLAB_LINKER) $(TARGETLAB_STARTUP)
else
TARGET_FLAGS := -D_POSIX_C_SOURCE=200809L -march=rv64gc -mabi=lp64d -mcmodel=medany
endif
TARGET_FLAGS += -DTARGETLAB_CLOCK_HZ=$(TARGETLAB_CLOCK_HZ) -DTARGETLAB_MINIMUM_CYCLES=$(TARGETLAB_MINIMUM_CYCLES)
ifeq ($(TARGETLAB_MEASUREMENT_MODE),qemu_proxy)
TARGET_FLAGS += -DTARGETLAB_QEMU_PROXY
endif

$(TARGETLAB_BUILD_DIR)/targetlab.elf: $(SOURCES)
	$(TARGETLAB_CC) -O2 -g -Wall -Wextra -Werror $(TARGET_FLAGS) $(SOURCES) -o $@

clean:
	rm -rf $(TARGETLAB_BUILD_DIR)
