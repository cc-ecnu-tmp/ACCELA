#!/usr/bin/env bash

set -u

dir="$(dirname "$0")"
compiler="${dir}/../../build/src/main"
if [ ! -x "$compiler" ]; then
  echo "Compiler not found: $compiler" >&2
  exit 1
fi

pass=0
fail=0
total=0

for sy in "$dir"/*.sy; do
  base="${sy%.sy}"
  asm="$base.s"
  elf="$base.elf"
  exp="$base.out"
  inp="$base.in"
  rm -f "$asm" "$elf" "$base.tmp"

  # if echo $base | grep -q "83" ; then
  #   echo "skip $base"
  #   continue
  # fi

  # num=$(echo "$base" | awk -F '[./_]' '{print $3}')
  # if [ "$num" -lt 94 ] ; then
  #     echo "skip $base"
  #     continue
  # fi
  # continue


  if timeout 30s "$compiler" "$sy" >/dev/null 2>&1 && \
    riscv64-linux-gnu-gcc "$asm" "$dir/../libsysy_riscv.a" \
        -static -o "$elf" >/dev/null 2>&1; then
    start=$(date +%s.%N)
    if [ -f "$inp" ]; then
      # qemu-riscv64 "$elf" < "$inp" >/dev/null
      timeout 30s qemu-riscv64 "$elf" < "$inp" >"$base.tmp" 2>/dev/null
    else
      timeout 30s qemu-riscv64 "$elf" >"$base.tmp"
    fi
    output=$?
    end=$(date +%s.%N)
    if [ -s "$base.tmp" ] && [ "$(tail -c1 "$base.tmp" | wc -l)" -eq 0 ]; then
      echo >> "$base.tmp"
    fi
    echo "$output" >> "$base.tmp"
    dos2unix "$base.tmp" >/dev/null 2>&1 #brainfk这个测试中会打印/r/n，但是我们是linux
    if diff -q "$base.tmp" "$exp" >/dev/null; then
      elapse=$(awk "BEGIN {print $end - $start}")
      echo "$(basename "$base"): SUCC" $elapse
      pass=$((pass+1))
    else
      echo "$(basename "$base"): FAIL (output mismatch)"
      diff "$base.tmp" "$exp" || true
      # echo "output:"
      # cat "$base.tmp"
      # echo "expect:"
      # cat "$exp"
      fail=$((fail+1))
    fi
  else
    echo "$(basename "$base"): FAIL (compile)" $?
    fail=$((fail+1))
  fi
  rm -f "$base.tmp" "$elf" "$asm"
  total=$((total+1))
done

echo "SUCC: $pass FAIL: $fail"
exit $fail
