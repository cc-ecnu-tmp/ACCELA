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
  .sdata : {
    __global_pointer$ = . + 0x800;
    *(.sdata .sdata.*)
  } > ram
  .targetlab.mailbox (NOLOAD) : ALIGN(64) { *(.targetlab.mailbox) } > ram
  . = ALIGN(64);
  __bss_start = .;
  .bss (NOLOAD) : { *(.bss .bss.* COMMON) } > ram
  __bss_end = .;
  . = ALIGN(16);
  __stack_top = ORIGIN(ram) + LENGTH(ram);
}
