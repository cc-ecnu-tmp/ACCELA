  .section .text.init
  .globl _start
_start:
  la gp, __global_pointer$
  la sp, __stack_top
  la t0, __bss_start
  la t1, __bss_end
.Lzero_bss:
  bgeu t0, t1, .Lrun_main
  sd zero, 0(t0)
  addi t0, t0, 8
  j .Lzero_bss
.Lrun_main:
  .word 0x12500013
  call main
  .word 0x12600013
  call qemu_exit
.Lhalt:
  wfi
  j .Lhalt
