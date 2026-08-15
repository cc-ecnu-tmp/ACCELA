OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
  ram (rwx) : ORIGIN = 0x80000000, LENGTH = 16M
}

SECTIONS {
  . = ORIGIN(ram);
  .text : { KEEP(*(.text.init)) *(.text .text.*) } > ram
  .rodata : { *(.rodata .rodata.*) } > ram
  .data : { *(.data .data.*) } > ram
  .targetlab.mailbox (NOLOAD) : ALIGN(64) { *(.targetlab.mailbox) } > ram
  .bss (NOLOAD) : ALIGN(64) { *(.bss .bss.* COMMON) } > ram
  . = ALIGN(16);
  __stack_top = ORIGIN(ram) + LENGTH(ram);
}
