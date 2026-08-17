OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
  ram (rwx) : ORIGIN = 0x80000000, LENGTH = 512M
}

SECTIONS {
  . = ORIGIN(ram);
  .text : {
    *(.text.init)
    *(.text .text.*)
  } > ram
  .rodata : { *(.rodata .rodata.*) } > ram
  .data : { *(.data .data.*) } > ram
  .sdata : {
    __global_pointer$ = . + 0x800;
    *(.sdata .sdata.*)
  } > ram
  . = ALIGN(64);
  .tohost : { KEEP(*(.tohost)) } > ram
  . = ALIGN(16);
  __bss_start = .;
  .bss : { *(.bss .bss.* COMMON) } > ram
  __bss_end = .;
  __stack_top = ORIGIN(ram) + LENGTH(ram) - 16;
}
