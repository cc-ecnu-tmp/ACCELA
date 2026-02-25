riscv64-linux-gnu-gcc -c sylib.c -o sylib.o -g 
riscv64-linux-gnu-ar rcs libsysy_g.a sylib.o
rm sylib.o
