.section .text.init
.globl _start
.type _start, @function
_start:
  la gp, __global_pointer$
  la sp, __stack_top
  la t0, __bss_start
  la t1, __bss_end
1:
  bgeu t0, t1, 2f
  sd zero, 0(t0)
  addi t0, t0, 8
  j 1b
2:
  li t0, 0x6000
  csrs mstatus, t0
  csrwi fcsr, 0
  call main
3:
  wfi
  j 3b
.size _start, .-_start
