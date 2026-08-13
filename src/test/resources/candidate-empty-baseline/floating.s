.attribute arch, "rv64gc"
.text

.globl main
main:
  addi sp, sp, -16
  sd ra, 0(sp)
.L_main_entry:
  call getfloat
  li a7, 1069547520
  fmv.w.x fa7, a7
  fmul.s ft0, fa0, fa7
  li a7, 1073741824
  fmv.w.x fa7, a7
  fadd.s fa0, ft0, fa7
  call putfloat
  li a0, 0
  ld ra, 0(sp)
  addi sp, sp, 16
  ret
