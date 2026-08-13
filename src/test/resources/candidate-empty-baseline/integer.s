.attribute arch, "rv64gc"
.text

.globl main
main:
  addi sp, sp, -16
  sd ra, 0(sp)
.L_main_entry:
  call getint
  sraiw a5, a0, 31
  srliw a5, a5, 29
  addw a0, a0, a5
  andi a0, a0, 7
  subw a0, a0, a5
  ld ra, 0(sp)
  addi sp, sp, 16
  ret
