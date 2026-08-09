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
  .bss (NOLOAD) : ALIGN(16) {
    __bss_start = .;
    *(.sbss .sbss.*)
    *(.bss .bss.* COMMON)
    . = ALIGN(16);
  } > ram
  .sysy_input_transport (NOLOAD) : ALIGN(16) {
    __sysy_input_transport_start = .;
    KEEP(*(.sysy_input_transport))
    KEEP(*(.sysy_input_transport.*))
    . = ALIGN(16);
    __sysy_input_transport_end = .;
  } > ram
  __bss_end = .;
  ASSERT(SIZEOF(.sysy_input_transport) == 4112,
         "invalid SysY input transport size")
  __stack_top = ORIGIN(ram) + LENGTH(ram) - 16;
}
