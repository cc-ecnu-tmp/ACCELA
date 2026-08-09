#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate the ACCELA clean-room SysY corpus and independent reference outputs.

The generator is deliberately self-contained and deterministic.  It does not
download, inspect, or fingerprint any official competition input.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
CORPUS_SEED = 20260809
STRUCTURE_VARIANT_SEED = 0x5A17C3


def i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def add32(lhs: int, rhs: int) -> int:
    return i32(lhs + rhs)


def sub32(lhs: int, rhs: int) -> int:
    return i32(lhs - rhs)


def mul32(lhs: int, rhs: int) -> int:
    return i32(lhs * rhs)


def div32(lhs: int, rhs: int) -> int:
    if rhs == 0:
        raise ZeroDivisionError("reference program attempted division by zero")
    if lhs == -0x80000000 and rhs == -1:
        return -0x80000000
    quotient = abs(lhs) // abs(rhs)
    return -quotient if (lhs < 0) != (rhs < 0) else quotient


def rem32(lhs: int, rhs: int) -> int:
    return sub32(lhs, mul32(div32(lhs, rhs), rhs))


def f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def fadd(lhs: float, rhs: float) -> float:
    return f32(f32(lhs) + f32(rhs))


def fsub(lhs: float, rhs: float) -> float:
    return f32(f32(lhs) - f32(rhs))


def fmul(lhs: float, rhs: float) -> float:
    return f32(f32(lhs) * f32(rhs))


def fdiv(lhs: float, rhs: float) -> float:
    return f32(f32(lhs) / f32(rhs))


def c_f32(value: float) -> float:
    """Independent binary32 conversion used to cross-check float references."""
    return ctypes.c_float(value).value


def c_fadd(lhs: float, rhs: float) -> float:
    return c_f32(c_f32(lhs) + c_f32(rhs))


def c_fsub(lhs: float, rhs: float) -> float:
    return c_f32(c_f32(lhs) - c_f32(rhs))


def c_fmul(lhs: float, rhs: float) -> float:
    return c_f32(c_f32(lhs) * c_f32(rhs))


def c_fdiv(lhs: float, rhs: float) -> float:
    return c_f32(c_f32(lhs) / c_f32(rhs))


def binary32_bits(value: float) -> bytes:
    return struct.pack("<f", value)


class Rng:
    """Small non-overflowing LCG mirrored exactly in generated SysY."""

    def __init__(self, seed: int):
        self.state = seed % 65521

    def next_small(self) -> int:
        self.state = (self.state * 25173 + 13849) % 65521
        return self.state % 31 - 15


def checksum_step(acc: int, value: int, index: int) -> int:
    return add32(add32(mul32(acc, 65599), value), mul32(index, 17))


COMMON_SYSY = """
int rng_state;

int next_small() {
  rng_state = (rng_state * 25173 + 13849) % 65521;
  return rng_state % 31 - 15;
}

int checksum_step(int acc, int value, int index) {
  return acc * 65599 + value + index * 17;
}
"""


def make_program(
    bench_id: str,
    description: str,
    globals_text: str,
    main_body: str,
    helpers: str = "",
) -> str:
    return f"""// SPDX-License-Identifier: MIT
// Provenance: ACCELA clean-room original; no upstream benchmark source copied.
// Family description: {description}
// Input contract: two decimal integers, problem size n and deterministic seed.

{globals_text.strip()}
{COMMON_SYSY.strip()}
{helpers.strip()}

int main() {{
  int n = getint();
  int seed = getint();
  rng_state = seed % 65521;
{indent(main_body.strip(), 2)}
}}
"""


def indent(text: str, amount: int) -> str:
    prefix = " " * amount
    return "\n".join(prefix + line if line else "" for line in text.splitlines())


@dataclass(frozen=True)
class BenchmarkSpec:
    bench_id: str
    group: str
    description: str
    sizes: tuple[int, int, int, int]
    source: Callable[[], str]
    reference: Callable[[int, int], int]
    reference_validation: tuple[str, str] | None = None


def matrix(n: int, rng: Rng, bias: int = 0) -> list[list[int]]:
    return [[rng.next_small() + bias for _ in range(n)] for _ in range(n)]


def vector(n: int, rng: Rng, bias: int = 0) -> list[int]:
    return [rng.next_small() + bias for _ in range(n)]


def checksum_matrix(values: list[list[int]]) -> int:
    acc = 0
    index = 0
    for row in values:
        for value in row:
            acc = checksum_step(acc, value, index)
            index += 1
    return acc


def checksum_vector(values: list[int]) -> int:
    acc = 0
    for index, value in enumerate(values):
        acc = checksum_step(acc, value, index)
    return acc


def src_pb_gemm() -> str:
    return make_program(
        "pb_gemm_i32",
        "dense integer GEMM with an accumulated output matrix",
        "int a[64][64]; int b[64][64]; int c[64][64];",
        """
int i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    a[i][j] = next_small();
    b[i][j] = next_small();
    c[i][j] = next_small();
    j = j + 1;
  }
  i = i + 1;
}
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    int k = 0;
    int sum = c[i][j];
    while (k < n) {
      sum = sum + a[i][k] * b[k][j];
      k = k + 1;
    }
    c[i][j] = sum;
    j = j + 1;
  }
  i = i + 1;
}
int acc = 0;
int index = 0;
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    acc = checksum_step(acc, c[i][j], index);
    index = index + 1;
    j = j + 1;
  }
  i = i + 1;
}
putint(acc); putch(10); return 0;
""",
    )


def ref_pb_gemm(n: int, seed: int) -> int:
    rng = Rng(seed)
    a = [[0] * n for _ in range(n)]
    b = [[0] * n for _ in range(n)]
    c = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            a[i][j] = rng.next_small()
            b[i][j] = rng.next_small()
            c[i][j] = rng.next_small()
    for i in range(n):
        for j in range(n):
            total = c[i][j]
            for k in range(n):
                total = add32(total, mul32(a[i][k], b[k][j]))
            c[i][j] = total
    return checksum_matrix(c)


def src_pb_gemver() -> str:
    return make_program(
        "pb_gemver_i32",
        "rank-two matrix update followed by transpose and forward products",
        """int a[96][96]; int u1[96]; int v1[96]; int u2[96]; int v2[96];
int x[96]; int y[96]; int z[96]; int w[96];""",
        """
int i = 0;
while (i < n) {
  u1[i] = next_small(); v1[i] = next_small();
  u2[i] = next_small(); v2[i] = next_small();
  y[i] = next_small(); z[i] = next_small();
  x[i] = 0; w[i] = 0;
  int j = 0;
  while (j < n) { a[i][j] = next_small(); j = j + 1; }
  i = i + 1;
}
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    a[i][j] = a[i][j] + u1[i] * v1[j] + u2[i] * v2[j];
    j = j + 1;
  }
  i = i + 1;
}
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { x[i] = x[i] + a[j][i] * y[j]; j = j + 1; }
  x[i] = x[i] + z[i];
  i = i + 1;
}
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { w[i] = w[i] + a[i][j] * x[j]; j = j + 1; }
  i = i + 1;
}
int acc = 0;
i = 0;
while (i < n) {
  acc = checksum_step(acc, w[i], i);
  acc = checksum_step(acc, x[i], i + n);
  i = i + 1;
}
putint(acc); putch(10); return 0;
""",
    )


def ref_pb_gemver(n: int, seed: int) -> int:
    rng = Rng(seed)
    a = [[0] * n for _ in range(n)]
    u1 = [0] * n
    v1 = [0] * n
    u2 = [0] * n
    v2 = [0] * n
    x = [0] * n
    y = [0] * n
    z = [0] * n
    w = [0] * n
    for i in range(n):
        u1[i], v1[i] = rng.next_small(), rng.next_small()
        u2[i], v2[i] = rng.next_small(), rng.next_small()
        y[i], z[i] = rng.next_small(), rng.next_small()
        for j in range(n):
            a[i][j] = rng.next_small()
    for i in range(n):
        for j in range(n):
            a[i][j] = add32(
                add32(a[i][j], mul32(u1[i], v1[j])), mul32(u2[i], v2[j])
            )
    for i in range(n):
        for j in range(n):
            x[i] = add32(x[i], mul32(a[j][i], y[j]))
        x[i] = add32(x[i], z[i])
    for i in range(n):
        for j in range(n):
            w[i] = add32(w[i], mul32(a[i][j], x[j]))
    acc = 0
    for i in range(n):
        acc = checksum_step(acc, w[i], i)
        acc = checksum_step(acc, x[i], i + n)
    return acc


def src_two_matrix_vector(bench_id: str, mode: str) -> str:
    descriptions = {
        "gesummv": "two dense matrix-vector products combined in one output",
        "mvt": "forward and transposed matrix-vector updates",
        "atax": "matrix-vector product followed by a transposed product",
        "bicg": "paired forward and transposed matrix-vector products",
    }
    globals_text = (
        "int a[128][128]; int b[128][128]; int x[128]; int y[128]; "
        "int p[128]; int r[128]; int q[128]; int s[128]; int tmp[128];"
    )
    init = """
int i = 0;
while (i < n) {
  x[i] = next_small(); y[i] = next_small();
  p[i] = next_small(); r[i] = next_small();
  q[i] = 0; s[i] = 0; tmp[i] = 0;
  int j = 0;
  while (j < n) {
    a[i][j] = next_small(); b[i][j] = next_small();
    j = j + 1;
  }
  i = i + 1;
}
"""
    if mode == "gesummv":
        compute = """
i = 0;
while (i < n) {
  int j = 0; int ta = 0; int tb = 0;
  while (j < n) {
    ta = ta + a[i][j] * x[j];
    tb = tb + b[i][j] * x[j];
    j = j + 1;
  }
  y[i] = 3 * ta + 2 * tb;
  i = i + 1;
}
int acc = 0; i = 0;
while (i < n) { acc = checksum_step(acc, y[i], i); i = i + 1; }
"""
    elif mode == "mvt":
        compute = """
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    x[i] = x[i] + a[i][j] * y[j];
    p[i] = p[i] + a[j][i] * r[j];
    j = j + 1;
  }
  i = i + 1;
}
int acc = 0; i = 0;
while (i < n) {
  acc = checksum_step(acc, x[i], i);
  acc = checksum_step(acc, p[i], i + n);
  i = i + 1;
}
"""
    elif mode == "atax":
        compute = """
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { tmp[i] = tmp[i] + a[i][j] * x[j]; j = j + 1; }
  i = i + 1;
}
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { y[i] = y[i] + a[j][i] * tmp[j]; j = j + 1; }
  i = i + 1;
}
int acc = 0; i = 0;
while (i < n) { acc = checksum_step(acc, y[i], i); i = i + 1; }
"""
    else:
        compute = """
i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    q[i] = q[i] + a[i][j] * p[j];
    s[i] = s[i] + a[j][i] * r[j];
    j = j + 1;
  }
  i = i + 1;
}
int acc = 0; i = 0;
while (i < n) {
  acc = checksum_step(acc, q[i], i);
  acc = checksum_step(acc, s[i], i + n);
  i = i + 1;
}
"""
    return make_program(bench_id, descriptions[mode], globals_text, init + compute + """
putint(acc); putch(10); return 0;
""")


def ref_two_matrix_vector(mode: str, n: int, seed: int) -> int:
    rng = Rng(seed)
    a = [[0] * n for _ in range(n)]
    b = [[0] * n for _ in range(n)]
    x = [0] * n
    y = [0] * n
    p = [0] * n
    r = [0] * n
    q = [0] * n
    s = [0] * n
    tmp = [0] * n
    for i in range(n):
        x[i], y[i] = rng.next_small(), rng.next_small()
        p[i], r[i] = rng.next_small(), rng.next_small()
        for j in range(n):
            a[i][j], b[i][j] = rng.next_small(), rng.next_small()
    if mode == "gesummv":
        for i in range(n):
            ta = tb = 0
            for j in range(n):
                ta = add32(ta, mul32(a[i][j], x[j]))
                tb = add32(tb, mul32(b[i][j], x[j]))
            y[i] = add32(mul32(3, ta), mul32(2, tb))
        return checksum_vector(y)
    if mode == "mvt":
        for i in range(n):
            for j in range(n):
                x[i] = add32(x[i], mul32(a[i][j], y[j]))
                p[i] = add32(p[i], mul32(a[j][i], r[j]))
        acc = 0
        for i in range(n):
            acc = checksum_step(acc, x[i], i)
            acc = checksum_step(acc, p[i], i + n)
        return acc
    if mode == "atax":
        for i in range(n):
            for j in range(n):
                tmp[i] = add32(tmp[i], mul32(a[i][j], x[j]))
        for i in range(n):
            for j in range(n):
                y[i] = add32(y[i], mul32(a[j][i], tmp[j]))
        return checksum_vector(y)
    for i in range(n):
        for j in range(n):
            q[i] = add32(q[i], mul32(a[i][j], p[j]))
            s[i] = add32(s[i], mul32(a[j][i], r[j]))
    acc = 0
    for i in range(n):
        acc = checksum_step(acc, q[i], i)
        acc = checksum_step(acc, s[i], i + n)
    return acc










def src_stencil(bench_id: str) -> str:
    if bench_id == "pb_jacobi1d_i32":
        return make_program(
            bench_id,
            "one-dimensional Jacobi time stencil",
            "int a[160]; int b[160];",
            """
int i = 0;
while (i < n) { a[i] = next_small() + 32; b[i] = 0; i = i + 1; }
int steps = n / 4 + 1; int t = 0;
while (t < steps) {
  i = 1;
  while (i + 1 < n) { b[i] = (a[i - 1] + a[i] + a[i + 1]) / 3; i = i + 1; }
  i = 1;
  while (i + 1 < n) { a[i] = b[i]; i = i + 1; }
  t = t + 1;
}
int acc = 0; i = 0;
while (i < n) { acc = checksum_step(acc, a[i], i); i = i + 1; }
putint(acc); putch(10); return 0;
""",
        )
    if bench_id == "pb_jacobi2d_i32":
        return make_program(
            bench_id,
            "two-dimensional five-point Jacobi time stencil",
            "int a[64][64]; int b[64][64];",
            """
int i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { a[i][j] = next_small() + 32; b[i][j] = 0; j = j + 1; }
  i = i + 1;
}
int steps = n / 4 + 1; int t = 0;
while (t < steps) {
  i = 1;
  while (i + 1 < n) {
    int j = 1;
    while (j + 1 < n) {
      b[i][j] = (a[i][j] + a[i-1][j] + a[i+1][j] + a[i][j-1] + a[i][j+1]) / 5;
      j = j + 1;
    }
    i = i + 1;
  }
  i = 1;
  while (i + 1 < n) {
    int j = 1;
    while (j + 1 < n) { a[i][j] = b[i][j]; j = j + 1; }
    i = i + 1;
  }
  t = t + 1;
}
int acc = 0; int index = 0; i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { acc = checksum_step(acc, a[i][j], index); index = index + 1; j = j + 1; }
  i = i + 1;
}
putint(acc); putch(10); return 0;
""",
        )
    if bench_id == "pb_seidel2d_i32":
        return make_program(
            bench_id,
            "in-place two-dimensional nine-point Seidel stencil",
            "int a[64][64];",
            """
int i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { a[i][j] = next_small() + 32; j = j + 1; }
  i = i + 1;
}
int steps = n / 5 + 1; int t = 0;
while (t < steps) {
  i = 1;
  while (i + 1 < n) {
    int j = 1;
    while (j + 1 < n) {
      a[i][j] = (a[i-1][j-1] + a[i-1][j] + a[i-1][j+1] +
                 a[i][j-1] + a[i][j] + a[i][j+1] +
                 a[i+1][j-1] + a[i+1][j] + a[i+1][j+1]) / 9;
      j = j + 1;
    }
    i = i + 1;
  }
  t = t + 1;
}
int acc = 0; int index = 0; i = 0;
while (i < n) {
  int j = 0;
  while (j < n) { acc = checksum_step(acc, a[i][j], index); index = index + 1; j = j + 1; }
  i = i + 1;
}
putint(acc); putch(10); return 0;
""",
        )
    raise ValueError(bench_id)


def ref_stencil(bench_id: str, n: int, seed: int) -> int:
    rng = Rng(seed)
    if bench_id == "pb_jacobi1d_i32":
        a = [rng.next_small() + 32 for _ in range(n)]
        b = [0] * n
        for _ in range(n // 4 + 1):
            for i in range(1, n - 1):
                b[i] = div32(add32(add32(a[i - 1], a[i]), a[i + 1]), 3)
            for i in range(1, n - 1):
                a[i] = b[i]
        return checksum_vector(a)
    a = matrix(n, rng, 32)
    if bench_id == "pb_jacobi2d_i32":
        b = [[0] * n for _ in range(n)]
        for _ in range(n // 4 + 1):
            for i in range(1, n - 1):
                for j in range(1, n - 1):
                    total = a[i][j]
                    total = add32(total, a[i - 1][j])
                    total = add32(total, a[i + 1][j])
                    total = add32(total, a[i][j - 1])
                    total = add32(total, a[i][j + 1])
                    b[i][j] = div32(total, 5)
            for i in range(1, n - 1):
                for j in range(1, n - 1):
                    a[i][j] = b[i][j]
    else:
        for _ in range(n // 5 + 1):
            for i in range(1, n - 1):
                for j in range(1, n - 1):
                    total = 0
                    for di in (-1, 0, 1):
                        for dj in (-1, 0, 1):
                            total = add32(total, a[i + di][j + dj])
                    a[i][j] = div32(total, 9)
    return checksum_matrix(a)


def src_pb_fdtd2d() -> str:
    return make_program(
        "pb_fdtd2d_i32",
        "coupled two-dimensional finite-difference field update",
        "int ex[64][64]; int ey[64][64]; int hz[64][64];",
        """
int i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    ex[i][j] = next_small() + 32; ey[i][j] = next_small() + 32; hz[i][j] = next_small() + 32;
    j = j + 1;
  }
  i = i + 1;
}
int steps = n / 4 + 1; int t = 0;
while (t < steps) {
  i = 1;
  while (i < n) {
    int j = 0;
    while (j < n) { ey[i][j] = ey[i][j] - (hz[i][j] - hz[i-1][j]) / 2; j = j + 1; }
    i = i + 1;
  }
  i = 0;
  while (i < n) {
    int j = 1;
    while (j < n) { ex[i][j] = ex[i][j] - (hz[i][j] - hz[i][j-1]) / 2; j = j + 1; }
    i = i + 1;
  }
  i = 0;
  while (i + 1 < n) {
    int j = 0;
    while (j + 1 < n) {
      hz[i][j] = hz[i][j] - ((ex[i][j+1] - ex[i][j]) + (ey[i+1][j] - ey[i][j])) / 3;
      j = j + 1;
    }
    i = i + 1;
  }
  t = t + 1;
}
int acc = 0; int index = 0; i = 0;
while (i < n) {
  int j = 0;
  while (j < n) {
    acc = checksum_step(acc, ex[i][j], index);
    acc = checksum_step(acc, ey[i][j], index + n*n);
    acc = checksum_step(acc, hz[i][j], index + 2*n*n);
    index = index + 1; j = j + 1;
  }
  i = i + 1;
}
putint(acc); putch(10); return 0;
""",
    )


def ref_pb_fdtd2d(n: int, seed: int) -> int:
    rng = Rng(seed)
    ex = [[0] * n for _ in range(n)]
    ey = [[0] * n for _ in range(n)]
    hz = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            ex[i][j] = rng.next_small() + 32
            ey[i][j] = rng.next_small() + 32
            hz[i][j] = rng.next_small() + 32
    for _ in range(n // 4 + 1):
        for i in range(1, n):
            for j in range(n):
                ey[i][j] = sub32(ey[i][j], div32(sub32(hz[i][j], hz[i - 1][j]), 2))
        for i in range(n):
            for j in range(1, n):
                ex[i][j] = sub32(ex[i][j], div32(sub32(hz[i][j], hz[i][j - 1]), 2))
        for i in range(n - 1):
            for j in range(n - 1):
                delta = add32(
                    sub32(ex[i][j + 1], ex[i][j]),
                    sub32(ey[i + 1][j], ey[i][j]),
                )
                hz[i][j] = sub32(hz[i][j], div32(delta, 3))
    acc = 0
    index = 0
    for i in range(n):
        for j in range(n):
            acc = checksum_step(acc, ex[i][j], index)
            acc = checksum_step(acc, ey[i][j], index + n * n)
            acc = checksum_step(acc, hz[i][j], index + 2 * n * n)
            index += 1
    return acc






















def src_eb_state_machine() -> str:
    return make_program(
        "eb_state_machine",
        "small branch-dense deterministic control-state machine",
        "",
        """
int state = seed % 11; int acc = 0; int i = 0;
while (i < n) {
  int event = next_small();
  if (event < 0) { event = 0 - event; }
  event = event % 5;
  if (state == 0 || state == 3) { state = (state + event + 1) % 11; }
  else {
    if (state < 6) { state = (state * 3 + event) % 11; }
    else { state = (state + 11 - event) % 11; }
  }
  if (event == 0) { acc = acc + state * 17; }
  else { acc = acc * 33 + state - event; }
  i = i + 1;
}
putint(acc); putch(10); return 0;
""",
    )


def ref_eb_state_machine(n: int, seed: int) -> int:
    rng = Rng(seed)
    state = seed % 11
    acc = 0
    for _ in range(n):
        event = abs(rng.next_small()) % 5
        if state in (0, 3):
            state = (state + event + 1) % 11
        elif state < 6:
            state = (state * 3 + event) % 11
        else:
            state = (state + 11 - event) % 11
        if event == 0:
            acc = add32(acc, mul32(state, 17))
        else:
            acc = sub32(add32(mul32(acc, 33), state), event)
    return acc














def src_pb_2mm() -> str:
    return make_program(
        "pb_2mm_i32",
        "two chained dense matrix multiplications",
        "int a[48][48]; int b[48][48]; int c[48][48]; int temp[48][48]; int out[48][48];",
        """
int i=0;
while(i<n){int j=0;while(j<n){a[i][j]=next_small();b[i][j]=next_small();c[i][j]=next_small();temp[i][j]=0;out[i][j]=0;j=j+1;}i=i+1;}
i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){temp[i][j]=temp[i][j]+a[i][k]*b[k][j];k=k+1;}j=j+1;}i=i+1;}
i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){out[i][j]=out[i][j]+temp[i][k]*c[k][j];k=k+1;}j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,out[i][j],index);index=index+1;j=j+1;}i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def ref_pb_2mm(n: int, seed: int) -> int:
    rng = Rng(seed)
    a = [[0] * n for _ in range(n)]
    b = [[0] * n for _ in range(n)]
    c = [[0] * n for _ in range(n)]
    temp = [[0] * n for _ in range(n)]
    out = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            a[i][j], b[i][j], c[i][j] = rng.next_small(), rng.next_small(), rng.next_small()
    for i in range(n):
        for j in range(n):
            for k in range(n):
                temp[i][j] = add32(temp[i][j], mul32(a[i][k], b[k][j]))
    for i in range(n):
        for j in range(n):
            for k in range(n):
                out[i][j] = add32(out[i][j], mul32(temp[i][k], c[k][j]))
    return checksum_matrix(out)


def src_pb_3mm() -> str:
    return make_program(
        "pb_3mm_i32",
        "three dense matrix products arranged as two producers and one consumer",
        """int a[40][40]; int b[40][40]; int c[40][40]; int d[40][40];
int e[40][40]; int f[40][40]; int g[40][40];""",
        """
int i=0;while(i<n){int j=0;while(j<n){a[i][j]=next_small();b[i][j]=next_small();c[i][j]=next_small();d[i][j]=next_small();e[i][j]=0;f[i][j]=0;g[i][j]=0;j=j+1;}i=i+1;}
i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){e[i][j]=e[i][j]+a[i][k]*b[k][j];f[i][j]=f[i][j]+c[i][k]*d[k][j];k=k+1;}j=j+1;}i=i+1;}
i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){g[i][j]=g[i][j]+e[i][k]*f[k][j];k=k+1;}j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,g[i][j],index);index=index+1;j=j+1;}i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def ref_pb_3mm(n: int, seed: int) -> int:
    rng = Rng(seed)
    mats = [[[0] * n for _ in range(n)] for _ in range(4)]
    e = [[0] * n for _ in range(n)]
    f = [[0] * n for _ in range(n)]
    g = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            for matrix_value in range(4):
                mats[matrix_value][i][j] = rng.next_small()
    for i in range(n):
        for j in range(n):
            for k in range(n):
                e[i][j] = add32(e[i][j], mul32(mats[0][i][k], mats[1][k][j]))
                f[i][j] = add32(f[i][j], mul32(mats[2][i][k], mats[3][k][j]))
    for i in range(n):
        for j in range(n):
            for k in range(n):
                g[i][j] = add32(g[i][j], mul32(e[i][k], f[k][j]))
    return checksum_matrix(g)


def src_pb_dynprog() -> str:
    return make_program(
        "pb_dynprog_i32",
        "interval dynamic programming with cubic split search",
        "int cost[96][96]; int weight[96];",
        """
int i=0;while(i<n){weight[i]=next_small();if(weight[i]<0){weight[i]=0-weight[i];}weight[i]=weight[i]+1;int j=0;while(j<n){cost[i][j]=0;j=j+1;}i=i+1;}
int length=2;
while(length<=n){i=0;while(i+length<=n){int j=i+length-1;int best=2147483647;int k=i;while(k<j){int candidate=cost[i][k]+cost[k+1][j]+weight[i]*weight[k+1]*weight[j];if(candidate<best){best=candidate;}k=k+1;}cost[i][j]=best;i=i+1;}length=length+1;}
int acc=0;i=0;while(i<n){acc=checksum_step(acc,cost[i][n-1],i);i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def ref_pb_dynprog(n: int, seed: int) -> int:
    rng = Rng(seed)
    weight = [abs(rng.next_small()) + 1 for _ in range(n)]
    cost = [[0] * n for _ in range(n)]
    for length in range(2, n + 1):
        for i in range(n - length + 1):
            j = i + length - 1
            best = 0x7FFFFFFF
            for k in range(i, j):
                candidate = add32(
                    add32(cost[i][k], cost[k + 1][j]),
                    mul32(mul32(weight[i], weight[k + 1]), weight[j]),
                )
                if candidate < best:
                    best = candidate
            cost[i][j] = best
    acc = 0
    for i in range(n):
        acc = checksum_step(acc, cost[i][n - 1], i)
    return acc


def src_pb_durbin() -> str:
    return make_program(
        "pb_durbin_f32",
        "float32 triangular Durbin-style recurrence",
        "float r[128]; float y[128]; float z[128];",
        """
int i=0;while(i<n){r[i]=next_small()*0.00390625;y[i]=0.0;z[i]=0.0;i=i+1;}
float beta=1.0;float alpha=0.0-r[0];y[0]=alpha;int k=1;
while(k<n){
  beta=beta*(1.0-alpha*alpha);
  float sum=0.0;i=0;while(i<k){sum=sum+r[k-i-1]*y[i];i=i+1;}
  alpha=(0.0-r[k]-sum)/beta;
  i=0;while(i<k){z[i]=y[i]+alpha*y[k-i-1];i=i+1;}
  i=0;while(i<k){y[i]=z[i];i=i+1;}y[k]=alpha;k=k+1;
}
int acc=0;i=0;while(i<n){int q=y[i]*1048576.0;acc=checksum_step(acc,q,i);i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def durbin_struct_reference(n: int, seed: int) -> list[float]:
    """Durbin recurrence using struct-pack binary32 rounding."""
    rng = Rng(seed)
    r = [fmul(float(rng.next_small()), 0.00390625) for _ in range(n)]
    y = [f32(0.0)] * n
    z = [f32(0.0)] * n
    beta = f32(1.0)
    alpha = fsub(0.0, r[0])
    y[0] = alpha
    for k in range(1, n):
        beta = fmul(beta, fsub(1.0, fmul(alpha, alpha)))
        total = f32(0.0)
        for i in range(k):
            total = fadd(total, fmul(r[k - i - 1], y[i]))
        alpha = fdiv(fsub(fsub(0.0, r[k]), total), beta)
        for i in range(k):
            z[i] = fadd(y[i], fmul(alpha, y[k - i - 1]))
        for i in range(k):
            y[i] = z[i]
        y[k] = alpha
    return y


def durbin_ctypes_reference(n: int, seed: int) -> list[float]:
    """Independent Durbin recurrence using ctypes.c_float rounding."""
    rng = Rng(seed)
    coefficients: list[float] = []
    for _ in range(n):
        coefficients.append(c_fmul(float(rng.next_small()), 0.00390625))

    solution = [c_f32(0.0) for _ in range(n)]
    scratch = [c_f32(0.0) for _ in range(n)]
    scale = c_f32(1.0)
    reflection = c_fsub(0.0, coefficients[0])
    solution[0] = reflection

    order = 1
    while order < n:
        squared = c_fmul(reflection, reflection)
        scale = c_fmul(scale, c_fsub(1.0, squared))
        correlation = c_f32(0.0)
        offset = 0
        while offset < order:
            product = c_fmul(coefficients[order - offset - 1], solution[offset])
            correlation = c_fadd(correlation, product)
            offset += 1
        numerator = c_fsub(c_fsub(0.0, coefficients[order]), correlation)
        reflection = c_fdiv(numerator, scale)
        offset = 0
        while offset < order:
            scratch[offset] = c_fadd(
                solution[offset],
                c_fmul(reflection, solution[order - offset - 1]),
            )
            offset += 1
        solution[:order] = scratch[:order]
        solution[order] = reflection
        order += 1
    return solution


def ref_pb_durbin(n: int, seed: int) -> int:
    primary = durbin_struct_reference(n, seed)
    crosscheck = durbin_ctypes_reference(n, seed)
    for index, (lhs, rhs) in enumerate(zip(primary, crosscheck, strict=True)):
        if binary32_bits(lhs) != binary32_bits(rhs):
            raise RuntimeError(
                "binary32 Durbin reference disagreement at "
                f"n={n}, seed={seed}, index={index}: "
                f"struct={binary32_bits(lhs).hex()}, ctypes={binary32_bits(rhs).hex()}"
            )
    acc = 0
    for i, value in enumerate(primary):
        acc = checksum_step(acc, int(fmul(value, 1048576.0)), i)
    return acc


def src_eb_aha_mont64() -> str:
    return make_program(
        "eb_aha_mont64",
        "eight-byte-limb multiprecision multiply/reduce workload shaped like 64-bit Montgomery arithmetic",
        "int limb_a[8]; int limb_b[8]; int product[16]; int next_a[8]; int next_b[8];",
        """
int i=0;while(i<8){int x=next_small();if(x<0){x=0-x;}limb_a[i]=(x*17+i+seed)%256;x=next_small();if(x<0){x=0-x;}limb_b[i]=(x*13+3*i+seed)%256;i=i+1;}
int round=0;
while(round<n){
  i=0;while(i<16){product[i]=0;i=i+1;}
  i=0;while(i<8){int j=0;while(j<8){product[i+j]=product[i+j]+limb_a[i]*limb_b[j];j=j+1;}i=i+1;}
  i=0;while(i<15){product[i+1]=product[i+1]+product[i]/256;product[i]=product[i]%256;i=i+1;}
  i=0;while(i<8){next_a[i]=(product[i]+limb_a[(i+1)%8]+round)%256;next_b[i]=(product[i+8]+limb_b[(i+3)%8]+17)%256;i=i+1;}
  i=0;while(i<8){limb_a[i]=next_a[i];limb_b[i]=next_b[i];i=i+1;}
  round=round+1;
}
int acc=0;i=0;while(i<8){acc=checksum_step(acc,limb_a[i],i);acc=checksum_step(acc,limb_b[i],i+8);i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def ref_eb_aha_mont64(n: int, seed: int) -> int:
    rng = Rng(seed)
    a = [0] * 8
    b = [0] * 8
    for i in range(8):
        a[i] = (abs(rng.next_small()) * 17 + i + seed) % 256
        b[i] = (abs(rng.next_small()) * 13 + 3 * i + seed) % 256
    for round_index in range(n):
        product = [0] * 16
        for i in range(8):
            for j in range(8):
                product[i + j] += a[i] * b[j]
        for i in range(15):
            product[i + 1] += product[i] // 256
            product[i] %= 256
        old_a, old_b = a[:], b[:]
        for i in range(8):
            a[i] = (product[i] + old_a[(i + 1) % 8] + round_index) % 256
            b[i] = (product[i + 8] + old_b[(i + 3) % 8] + 17) % 256
    acc = 0
    for i in range(8):
        acc = checksum_step(acc, a[i], i)
        acc = checksum_step(acc, b[i], i + 8)
    return acc


def src_eb_crc32() -> str:
    return make_program(
        "eb_crc32",
        "32-bit polynomial CRC represented explicitly as SysY boolean bits",
        "int crc_bits[32]; int poly_bits[32];",
        """
int polynomial=79764919;int i=0;int power=1;
while(i<32){poly_bits[i]=(polynomial/power)%2;crc_bits[i]=1;i=i+1;if(i<31){power=power*2;}}
int byte_index=0;
while(byte_index<n){
  int value=next_small();if(value<0){value=0-value;}value=(value+byte_index)%256;
  i=0;power=1;while(i<8){crc_bits[24+i]=(crc_bits[24+i]+(value/power)%2)%2;power=power*2;i=i+1;}
  int bit=0;while(bit<8){int top=crc_bits[31];i=31;while(i>0){crc_bits[i]=crc_bits[i-1];i=i-1;}crc_bits[0]=0;if(top!=0){i=0;while(i<32){crc_bits[i]=(crc_bits[i]+poly_bits[i])%2;i=i+1;}}bit=bit+1;}
  byte_index=byte_index+1;
}
int acc=0;i=0;while(i<32){acc=checksum_step(acc,crc_bits[i],i);i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def ref_eb_crc32(n: int, seed: int) -> int:
    rng = Rng(seed)
    polynomial = 79764919
    poly = [(polynomial >> i) & 1 for i in range(32)]
    bits = [1] * 32
    for byte_index in range(n):
        value = (abs(rng.next_small()) + byte_index) % 256
        for i in range(8):
            bits[24 + i] ^= (value >> i) & 1
        for _ in range(8):
            top = bits[31]
            bits = [0] + bits[:31]
            if top:
                bits = [lhs ^ rhs for lhs, rhs in zip(bits, poly)]
    return checksum_vector(bits)


def src_eb_huffbench() -> str:
    return make_program(
        "eb_huffbench",
        "Huffman-style frequency collection and repeated minimum-node merge",
        "int frequency[64]; int active[128]; int weight[128];",
        """
int i=0;while(i<64){frequency[i]=0;i=i+1;}i=0;
while(i<n){int value=next_small();if(value<0){value=0-value;}value=(value+i)%64;frequency[value]=frequency[value]+1;i=i+1;}
int nodes=0;i=0;while(i<64){if(frequency[i]!=0){weight[nodes]=frequency[i];active[nodes]=1;nodes=nodes+1;}i=i+1;}
int total_nodes=nodes;int weighted_path=0;
while(nodes>1){
  int first=-1;int second=-1;i=0;
  while(i<total_nodes){if(active[i]!=0){if(first<0||weight[i]<weight[first]){second=first;first=i;}else{if(second<0||weight[i]<weight[second]){second=i;}}}i=i+1;}
  int merged=weight[first]+weight[second];weighted_path=weighted_path+merged;active[first]=0;active[second]=0;weight[total_nodes]=merged;active[total_nodes]=1;total_nodes=total_nodes+1;nodes=nodes-1;
}
int acc=checksum_step(weighted_path,total_nodes,n);putint(acc);putch(10);return 0;
""",
    )


def ref_eb_huffbench(n: int, seed: int) -> int:
    rng = Rng(seed)
    frequencies = [0] * 64
    for i in range(n):
        frequencies[(abs(rng.next_small()) + i) % 64] += 1
    weights = [value for value in frequencies if value]
    initial_nodes = len(weights)
    weighted_path = 0
    while len(weights) > 1:
        weights.sort()
        merged = weights.pop(0) + weights.pop(0)
        weighted_path += merged
        weights.append(merged)
    total_nodes = initial_nodes * 2 - 1 if initial_nodes else 0
    return checksum_step(weighted_path, total_nodes, n)


def src_eb_matmult_int() -> str:
    return make_program(
        "eb_matmult_int",
        "integer dense matrix multiplication with embedded-style checksum",
        "int a[48][48]; int b[48][48]; int c[48][48];",
        """
int i=0;while(i<n){int j=0;while(j<n){a[i][j]=next_small();b[i][j]=next_small();c[i][j]=0;j=j+1;}i=i+1;}
i=0;while(i<n){int k=0;while(k<n){int av=a[i][k];int j=0;while(j<n){c[i][j]=c[i][j]+av*b[k][j];j=j+1;}k=k+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,c[i][j],index);index=index+1;j=j+1;}i=i+1;}
putint(acc);putch(10);return 0;
""",
    )


def ref_eb_matmult_int(n: int, seed: int) -> int:
    rng = Rng(seed)
    a = [[0] * n for _ in range(n)]
    b = [[0] * n for _ in range(n)]
    c = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            a[i][j], b[i][j] = rng.next_small(), rng.next_small()
    for i in range(n):
        for k in range(n):
            for j in range(n):
                c[i][j] = add32(c[i][j], mul32(a[i][k], b[k][j]))
    return checksum_matrix(c)


AES_HELPERS = """
int xor_byte(int a,int b){int result=0;int power=1;int i=0;while(i<8){int bit=((a/power)%2+(b/power)%2)%2;result=result+bit*power;power=power*2;i=i+1;}return result;}
int xtime(int x){int high=x/128;int value=(x%128)*2;if(high!=0){value=xor_byte(value,27);}return value;}
int gf_mul(int a,int b){int result=0;int i=0;while(i<8){if(b%2!=0){result=xor_byte(result,a);}a=xtime(a);b=b/2;i=i+1;}return result;}
int gf_pow254(int value){if(value==0){return 0;}int result=1;int base=value;int exponent=254;while(exponent!=0){if(exponent%2!=0){result=gf_mul(result,base);}base=gf_mul(base,base);exponent=exponent/2;}return result;}
int rotl_byte(int value,int amount){int power=1;int result=0;int i=0;while(i<8){int bit=(value/power)%2;int target=(i+amount)%8;int target_power=1;int j=0;while(j<target){target_power=target_power*2;j=j+1;}result=result+bit*target_power;power=power*2;i=i+1;}return result;}
int sub_byte(int value){int inverse=gf_pow254(value);return xor_byte(xor_byte(xor_byte(xor_byte(inverse,rotl_byte(inverse,1)),rotl_byte(inverse,2)),rotl_byte(inverse,3)),xor_byte(rotl_byte(inverse,4),99));}
"""


def src_eb_nettle_aes() -> str:
    return make_program(
        "eb_nettle_aes",
        "AES-shaped byte substitution, row permutation, and GF(2^8) column mixing",
        "int state_bytes[16]; int temp_bytes[16]; int round_key[16];",
        """
int i=0;while(i<16){int value=next_small();if(value<0){value=0-value;}state_bytes[i]=(value*13+i+seed)%256;value=next_small();if(value<0){value=0-value;}round_key[i]=(value*17+3*i)%256;i=i+1;}
int block=0;
while(block<n){
  int round=0;
  while(round<6){
    i=0;while(i<16){state_bytes[i]=sub_byte(xor_byte(state_bytes[i],round_key[i]));i=i+1;}
    i=0;while(i<16){int row=i/4;int col=i%4;temp_bytes[row*4+col]=state_bytes[row*4+(col+row)%4];i=i+1;}
    int col=0;while(col<4){int a0=temp_bytes[col];int a1=temp_bytes[4+col];int a2=temp_bytes[8+col];int a3=temp_bytes[12+col];
      state_bytes[col]=xor_byte(xor_byte(gf_mul(a0,2),gf_mul(a1,3)),xor_byte(a2,a3));
      state_bytes[4+col]=xor_byte(xor_byte(a0,gf_mul(a1,2)),xor_byte(gf_mul(a2,3),a3));
      state_bytes[8+col]=xor_byte(xor_byte(a0,a1),xor_byte(gf_mul(a2,2),gf_mul(a3,3)));
      state_bytes[12+col]=xor_byte(xor_byte(gf_mul(a0,3),a1),xor_byte(a2,gf_mul(a3,2)));col=col+1;}
    i=0;while(i<16){round_key[i]=(round_key[i]+17+round+i)%256;i=i+1;}round=round+1;
  }
  block=block+1;
}
int acc=0;i=0;while(i<16){acc=checksum_step(acc,state_bytes[i],i);i=i+1;}putint(acc);putch(10);return 0;
""",
        AES_HELPERS,
    )




def aes_xtime(value: int) -> int:
    return ((value << 1) ^ (0x1B if value & 0x80 else 0)) & 0xFF


def aes_mul(lhs: int, rhs: int) -> int:
    result = 0
    for _ in range(8):
        if rhs & 1:
            result ^= lhs
        lhs = aes_xtime(lhs)
        rhs >>= 1
    return result


def aes_sbox(value: int) -> int:
    if value == 0:
        inverse = 0
    else:
        inverse, base, exponent = 1, value, 254
        while exponent:
            if exponent & 1:
                inverse = aes_mul(inverse, base)
            base = aes_mul(base, base)
            exponent >>= 1
    result = inverse
    for amount in (1, 2, 3, 4):
        result ^= ((inverse << amount) | (inverse >> (8 - amount))) & 0xFF
    return result ^ 0x63


def ref_eb_nettle_aes(n: int, seed: int) -> int:
    rng = Rng(seed)
    state = [0] * 16
    key = [0] * 16
    for i in range(16):
        state[i] = (abs(rng.next_small()) * 13 + i + seed) % 256
        key[i] = (abs(rng.next_small()) * 17 + 3 * i) % 256
    for _ in range(n):
        for round_index in range(6):
            state = [aes_sbox(value ^ key[i]) for i, value in enumerate(state)]
            temp = [0] * 16
            for i in range(16):
                row, col = divmod(i, 4)
                temp[row * 4 + col] = state[row * 4 + (col + row) % 4]
            for col in range(4):
                a0, a1, a2, a3 = temp[col], temp[4 + col], temp[8 + col], temp[12 + col]
                state[col] = aes_mul(a0, 2) ^ aes_mul(a1, 3) ^ a2 ^ a3
                state[4 + col] = a0 ^ aes_mul(a1, 2) ^ aes_mul(a2, 3) ^ a3
                state[8 + col] = a0 ^ a1 ^ aes_mul(a2, 2) ^ aes_mul(a3, 3)
                state[12 + col] = aes_mul(a0, 3) ^ a1 ^ a2 ^ aes_mul(a3, 2)
            key = [(value + 17 + round_index + i) % 256 for i, value in enumerate(key)]
    return checksum_vector(state)


SHA_HELPERS = """
int xor16(int a,int b){int result=0;int power=1;int i=0;while(i<16){int bit=((a/power)%2+(b/power)%2)%2;result=result+bit*power;power=power*2;i=i+1;}return result;}
int and16(int a,int b){int result=0;int power=1;int i=0;while(i<16){int bit=(a/power)%2*(b/power)%2;result=result+bit*power;power=power*2;i=i+1;}return result;}
int not16(int a){return 65535-a;}
int rotr16(int value,int amount){int low_power=1;int i=0;while(i<amount){low_power=low_power*2;i=i+1;}int low=value%low_power;int high=value/low_power;int shift_power=1;i=0;while(i<16-amount){shift_power=shift_power*2;i=i+1;}return (low*shift_power+high)%65536;}
"""


def src_eb_nettle_sha256() -> str:
    return make_program(
        "eb_nettle_sha256",
        "SHA-256 control/dataflow analogue over explicit 16-bit lanes",
        "int state_words[8]; int schedule_words[32];",
        """
int i=0;while(i<8){state_words[i]=(seed*17+i*8191+12345)%65536;i=i+1;}
int block=0;
while(block<n){
  i=0;while(i<16){int value=next_small();if(value<0){value=0-value;}schedule_words[i]=(value*257+i*97+block)%65536;i=i+1;}
  i=16;while(i<32){schedule_words[i]=(xor16(rotr16(schedule_words[i-2],3),rotr16(schedule_words[i-7],7))+schedule_words[i-15]+schedule_words[i-16])%65536;i=i+1;}
  int a=state_words[0];int b=state_words[1];int c=state_words[2];int d=state_words[3];int e=state_words[4];int f=state_words[5];int g=state_words[6];int h=state_words[7];
  int round=0;while(round<32){int choose=xor16(and16(e,f),and16(not16(e),g));int majority=xor16(xor16(and16(a,b),and16(a,c)),and16(b,c));
    int sigma1=xor16(xor16(rotr16(e,2),rotr16(e,5)),rotr16(e,7));int sigma0=xor16(xor16(rotr16(a,1),rotr16(a,4)),rotr16(a,6));
    int t1=(h+sigma1+choose+schedule_words[round]+round*193)%65536;int t2=(sigma0+majority)%65536;
    h=g;g=f;f=e;e=(d+t1)%65536;d=c;c=b;b=a;a=(t1+t2)%65536;round=round+1;}
  state_words[0]=(state_words[0]+a)%65536;state_words[1]=(state_words[1]+b)%65536;state_words[2]=(state_words[2]+c)%65536;state_words[3]=(state_words[3]+d)%65536;
  state_words[4]=(state_words[4]+e)%65536;state_words[5]=(state_words[5]+f)%65536;state_words[6]=(state_words[6]+g)%65536;state_words[7]=(state_words[7]+h)%65536;block=block+1;
}
int acc=0;i=0;while(i<8){acc=checksum_step(acc,state_words[i],i);i=i+1;}putint(acc);putch(10);return 0;
""",
        SHA_HELPERS,
    )


def rotr16(value: int, amount: int) -> int:
    return ((value >> amount) | (value << (16 - amount))) & 0xFFFF


def ref_eb_nettle_sha256(n: int, seed: int) -> int:
    rng = Rng(seed)
    state = [(seed * 17 + i * 8191 + 12345) % 65536 for i in range(8)]
    for block in range(n):
        schedule = [0] * 32
        for i in range(16):
            schedule[i] = (abs(rng.next_small()) * 257 + i * 97 + block) % 65536
        for i in range(16, 32):
            schedule[i] = ((rotr16(schedule[i - 2], 3) ^ rotr16(schedule[i - 7], 7)) + schedule[i - 15] + schedule[i - 16]) % 65536
        a, b, c, d, e, f, g, h = state
        for round_index in range(32):
            choose = (e & f) ^ ((~e & 0xFFFF) & g)
            majority = (a & b) ^ (a & c) ^ (b & c)
            sigma1 = rotr16(e, 2) ^ rotr16(e, 5) ^ rotr16(e, 7)
            sigma0 = rotr16(a, 1) ^ rotr16(a, 4) ^ rotr16(a, 6)
            t1 = (h + sigma1 + choose + schedule[round_index] + round_index * 193) % 65536
            t2 = (sigma0 + majority) % 65536
            h, g, f, e, d, c, b, a = g, f, e, (d + t1) % 65536, c, b, a, (t1 + t2) % 65536
        state = [(state[i] + value) % 65536 for i, value in enumerate((a, b, c, d, e, f, g, h))]
    return checksum_vector(state)


def src_eb_statemate() -> str:
    source = src_eb_state_machine()
    return source.replace("eb_state_machine", "eb_statemate").replace(
        "small branch-dense deterministic control-state machine",
        "Statemate-shaped branch-dense deterministic control-state machine",
    )


def src_eb_wikisort() -> str:
    return make_program(
        "eb_wikisort",
        "stable bottom-up merge sort over records with duplicate keys",
        "int keys[4096]; int payload[4096]; int temp_keys[4096]; int temp_payload[4096];",
        """
int i=0;while(i<n){int value=next_small();keys[i]=value%97;payload[i]=i;i=i+1;}
int width=1;
while(width<n){int left=0;while(left<n){int mid=left+width;if(mid>n){mid=n;}int right=mid+width;if(right>n){right=n;}int a=left;int b=mid;int out=left;
  while(a<mid||b<right){if(b>=right||(a<mid&&keys[a]<=keys[b])){temp_keys[out]=keys[a];temp_payload[out]=payload[a];a=a+1;}else{temp_keys[out]=keys[b];temp_payload[out]=payload[b];b=b+1;}out=out+1;}
  left=right;}
  i=0;while(i<n){keys[i]=temp_keys[i];payload[i]=temp_payload[i];i=i+1;}width=width*2;
}
int acc=0;i=0;while(i<n){acc=checksum_step(acc,keys[i],i);acc=checksum_step(acc,payload[i],i+n);i=i+1;}putint(acc);putch(10);return 0;
""",
    )


def ref_eb_wikisort(n: int, seed: int) -> int:
    rng = Rng(seed)
    records = [(rem32(rng.next_small(), 97), i) for i in range(n)]
    records.sort(key=lambda item: item[0])
    acc = 0
    for i, (key, payload) in enumerate(records):
        acc = checksum_step(acc, key, i)
        acc = checksum_step(acc, payload, i + n)
    return acc


BENCHMARKS: tuple[BenchmarkSpec, ...] = (
    BenchmarkSpec("pb_2mm_i32", "polybench-style", "two chained matrix products", (2, 6, 16, 32), src_pb_2mm, ref_pb_2mm),
    BenchmarkSpec("pb_3mm_i32", "polybench-style", "three matrix products", (2, 4, 12, 24), src_pb_3mm, ref_pb_3mm),
    BenchmarkSpec("pb_atax_i32", "polybench-style", "A then transpose-A product", (4, 16, 48, 96), lambda: src_two_matrix_vector("pb_atax_i32", "atax"), lambda n, s: ref_two_matrix_vector("atax", n, s)),
    BenchmarkSpec("pb_bicg_i32", "polybench-style", "paired matrix-vector products", (4, 16, 48, 96), lambda: src_two_matrix_vector("pb_bicg_i32", "bicg"), lambda n, s: ref_two_matrix_vector("bicg", n, s)),
    BenchmarkSpec("pb_gemm_i32", "polybench-style", "dense GEMM", (4, 8, 24, 48), src_pb_gemm, ref_pb_gemm),
    BenchmarkSpec("pb_gemver_i32", "polybench-style", "rank-two update and products", (4, 12, 40, 80), src_pb_gemver, ref_pb_gemver),
    BenchmarkSpec("pb_gesummv_i32", "polybench-style", "combined matrix-vector products", (4, 16, 48, 96), lambda: src_two_matrix_vector("pb_gesummv_i32", "gesummv"), lambda n, s: ref_two_matrix_vector("gesummv", n, s)),
    BenchmarkSpec("pb_mvt_i32", "polybench-style", "forward/transposed products", (4, 16, 48, 96), lambda: src_two_matrix_vector("pb_mvt_i32", "mvt"), lambda n, s: ref_two_matrix_vector("mvt", n, s)),
    BenchmarkSpec("pb_dynprog_i32", "polybench-style", "interval dynamic programming", (4, 8, 24, 48), src_pb_dynprog, ref_pb_dynprog),
    BenchmarkSpec(
        "pb_durbin_f32", "polybench-style", "float32 triangular Durbin recurrence",
        (4, 8, 32, 96), src_pb_durbin, ref_pb_durbin,
        reference_validation=("python-struct-binary32", "ctypes-c-float-binary32"),
    ),
    BenchmarkSpec("pb_jacobi1d_i32", "polybench-style", "one-dimensional Jacobi", (6, 16, 64, 128), lambda: src_stencil("pb_jacobi1d_i32"), lambda n, s: ref_stencil("pb_jacobi1d_i32", n, s)),
    BenchmarkSpec("pb_jacobi2d_i32", "polybench-style", "two-dimensional Jacobi", (4, 8, 24, 48), lambda: src_stencil("pb_jacobi2d_i32"), lambda n, s: ref_stencil("pb_jacobi2d_i32", n, s)),
    BenchmarkSpec("pb_seidel2d_i32", "polybench-style", "two-dimensional Seidel", (4, 8, 24, 48), lambda: src_stencil("pb_seidel2d_i32"), lambda n, s: ref_stencil("pb_seidel2d_i32", n, s)),
    BenchmarkSpec("pb_fdtd2d_i32", "polybench-style", "coupled finite-difference fields", (4, 8, 24, 48), src_pb_fdtd2d, ref_pb_fdtd2d),
    BenchmarkSpec("eb_aha_mont64", "embench-style", "multiprecision Montgomery-shaped arithmetic", (1, 8, 1000, 100000), src_eb_aha_mont64, ref_eb_aha_mont64),
    BenchmarkSpec("eb_crc32", "embench-style", "explicit-bit polynomial CRC32", (4, 16, 512, 4096), src_eb_crc32, ref_eb_crc32),
    BenchmarkSpec("eb_huffbench", "embench-style", "Huffman merge workload", (16, 128, 4096, 32768), src_eb_huffbench, ref_eb_huffbench),
    BenchmarkSpec("eb_matmult_int", "embench-style", "integer matrix multiplication", (4, 8, 24, 48), src_eb_matmult_int, ref_eb_matmult_int),
    BenchmarkSpec("eb_nettle_aes", "embench-style", "AES-shaped byte cipher workload", (1, 4, 16, 128), src_eb_nettle_aes, ref_eb_nettle_aes),
    BenchmarkSpec("eb_nettle_sha256", "embench-style", "SHA-256-shaped compression workload", (1, 4, 64, 512), src_eb_nettle_sha256, ref_eb_nettle_sha256),
    BenchmarkSpec("eb_statemate", "embench-style", "control-state machine", (16, 1000, 100000, 1000000), src_eb_statemate, ref_eb_state_machine),
    BenchmarkSpec("eb_wikisort", "embench-style", "stable bottom-up merge sort", (8, 32, 512, 4096), src_eb_wikisort, ref_eb_wikisort),
)


def derive_seed(root_seed: int, identifier: str, tier: str) -> int:
    digest = hashlib.sha256(f"{root_seed}:{identifier}:{tier}".encode()).digest()
    return int.from_bytes(digest[:4], "little") % 60000 + 1


def stable_seed(identifier: str, tier: str) -> int:
    return derive_seed(CORPUS_SEED, identifier, tier)


def stable_structure_seed(identifier: str, tier: str) -> int:
    return derive_seed(STRUCTURE_VARIANT_SEED, identifier, tier)


def emit(path: Path, content: str, check: bool, failures: list[str]) -> None:
    normalized = content.replace("\r\n", "\n")
    if check:
        if not path.exists():
            failures.append(f"missing: {path.relative_to(ROOT)}")
        elif path.read_text(encoding="utf-8").replace("\r\n", "\n") != normalized:
            failures.append(f"stale: {path.relative_to(ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized, encoding="utf-8", newline="\n")


def build_manifest(
    oracle_manifest: list[dict[str, object]],
    structural_manifest: list[dict[str, object]],
) -> dict[str, object]:
    benchmarks: list[dict[str, object]] = []
    tiers = ("correctness", "small", "medium", "large")
    for spec in BENCHMARKS:
        datasets = []
        for tier, n in zip(tiers, spec.sizes, strict=True):
            seed = stable_seed(spec.bench_id, tier)
            datasets.append(
                {
                    "tier": tier,
                    "role": "correctness" if tier == "correctness" else "performance",
                    "n": n,
                    "seed": seed,
                    "input": f"datasets/{spec.bench_id}/{tier}.in",
                    "output": f"datasets/{spec.bench_id}/{tier}.out",
                }
            )
        benchmark = {
            "id": spec.bench_id,
            "group": spec.group,
            "description": spec.description,
            "source": f"programs/{spec.bench_id}.sy",
            "spdx": "MIT",
            "provenance": "ACCELA clean-room original; taxonomy only, no upstream source or dataset copied",
            "datasets": datasets,
        }
        if spec.reference_validation is not None:
            benchmark["reference_validation"] = list(spec.reference_validation)
        benchmarks.append(benchmark)
    return {
        "schema_version": 1,
        "corpus_seed": CORPUS_SEED,
        "structure_variant_seed": STRUCTURE_VARIANT_SEED,
        "generator": "generate.py",
        "integer_semantics": "signed 32-bit two's-complement wrap; division truncates toward zero",
        "float_semantics": "IEEE-754 binary32, rounded after every source-level operation",
        "output_contract": (
            "exact program stdout bytes followed by an LF-delimited decimal uint8 main return code"
        ),
        "provenance_policy": {
            "license": "MIT",
            "origin": "clean-room",
            "forbidden_features": [
                "official case fingerprints",
                "source or function name hashes",
                "absolute local paths",
                "copied upstream benchmark source",
            ],
        },
        "benchmarks": benchmarks,
        "oracle_families": oracle_manifest,
        "structural_variants": structural_manifest,
    }


def generate(check: bool) -> list[str]:
    failures: list[str] = []
    tiers = ("correctness", "small", "medium", "large")
    for spec in BENCHMARKS:
        emit(ROOT / "programs" / f"{spec.bench_id}.sy", spec.source(), check, failures)
        for tier, n in zip(tiers, spec.sizes, strict=True):
            seed = stable_seed(spec.bench_id, tier)
            value = spec.reference(n, seed)
            emit(ROOT / "datasets" / spec.bench_id / f"{tier}.in", f"{n} {seed}\n", check, failures)
            emit(ROOT / "datasets" / spec.bench_id / f"{tier}.out", f"{value}\n0\n", check, failures)
    oracle_manifest = generate_oracles(check, failures)
    structural_manifest = generate_structural_variants(check, failures)
    manifest = json.dumps(
        build_manifest(oracle_manifest, structural_manifest), indent=2, sort_keys=False
    ) + "\n"
    emit(ROOT / "manifest.json", manifest, check, failures)
    return failures


# Oracle-pair generation is defined below.  Each pair is independently
# executable SysY and has three deterministic datasets.


@dataclass(frozen=True)
class OracleSpec:
    family: str
    variant: str
    description: str
    sizes: tuple[int, int, int]
    baseline: str
    optimized: str
    reference: Callable[[int, int], int]

    @property
    def identifier(self) -> str:
        return f"{self.family}/{self.variant}"


def make_oracle(
    family: str,
    variant: str,
    role: str,
    description: str,
    body: str,
    globals_text: str = "",
    helpers: str = "",
) -> str:
    return make_program(
        f"{family}_{variant}_{role}",
        f"semantic oracle for {family}/{variant}: {description}; role={role}",
        globals_text,
        body,
        helpers,
    )


def oracle_closed_form() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    bodies = [
        (
            "linear_sum",
            "linear induction-dependent sum",
            (8, 80, 800),
            """
int x = seed; int i = 0;
while (i < n) { x = x + 3 * i + 5; i = i + 1; }
putint(x); putch(10); return 0;
""",
            """
int triangular = n * (n - 1) / 2;
int x = seed + 3 * triangular + 5 * n;
putint(x); putch(10); return 0;
""",
            lambda n, seed: add32(seed, add32(mul32(3, n * (n - 1) // 2), mul32(5, n))),
        ),
        (
            "quadratic_sum",
            "degree-two induction polynomial",
            (6, 32, 128),
            """
int x = seed; int i = 0;
while (i < n) { x = x + i * i + 2 * i + 1; i = i + 1; }
putint(x); putch(10); return 0;
""",
            """
int squares = n * (n - 1) * (2 * n - 1) / 6;
int linear = n * (n - 1);
int x = seed + squares + linear + n;
putint(x); putch(10); return 0;
""",
            lambda n, seed: add32(seed, sum((i * i + 2 * i + 1) for i in range(n))),
        ),
        (
            "triangular_recurrence",
            "two ordered scalar recurrences",
            (8, 64, 512),
            """
int x = seed; int y = 0; int i = 0;
while (i < n) { x = x + 2; y = y + x; i = i + 1; }
putint(y); putch(10); return 0;
""",
            """
int y = n * seed + n * (n + 1);
putint(y); putch(10); return 0;
""",
            lambda n, seed: add32(mul32(n, seed), mul32(n, n + 1)),
        ),
    ]
    for variant, desc, sizes, baseline_body, optimized_body, reference in bodies:
        result.append(
            OracleSpec(
                "closed_form",
                variant,
                desc,
                sizes,
                make_oracle("closed_form", variant, "baseline", desc, baseline_body),
                make_oracle("closed_form", variant, "optimized", desc, optimized_body),
                reference,
            )
        )
    return result


def oracle_dp_storage() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    globals_full = "int table[64][64];"
    globals_roll = "int row[3][64];"
    baseline1 = """
int i = 0;
while (i < n) { table[i][0] = seed + i; table[0][i] = seed - i; i = i + 1; }
i = 1;
while (i < n) {
  int j = 1;
  while (j < n) {
    table[i][j] = table[i-1][j] + table[i][j-1] + (i*j + seed) % 7;
    j = j + 1;
  }
  i = i + 1;
}
putint(table[n-1][n-1]); putch(10); return 0;
"""
    optimized1 = """
int j = 0;
while (j < n) { row[0][j] = seed - j; j = j + 1; }
int i = 1;
while (i < n) {
  int cur = i % 2; int prev = 1 - cur; row[cur][0] = seed + i; j = 1;
  while (j < n) {
    row[cur][j] = row[prev][j] + row[cur][j-1] + (i*j + seed) % 7;
    j = j + 1;
  }
  i = i + 1;
}
putint(row[(n-1)%2][n-1]); putch(10); return 0;
"""
    def ref1(n: int, seed: int) -> int:
        table = [[0] * n for _ in range(n)]
        for i in range(n):
            table[i][0], table[0][i] = add32(seed, i), sub32(seed, i)
        for i in range(1, n):
            for j in range(1, n):
                table[i][j] = add32(
                    add32(table[i - 1][j], table[i][j - 1]), (i * j + seed) % 7
                )
        return table[-1][-1]
    result.append(OracleSpec(
        "dp_storage", "two_row", "full table versus two-row frontier", (4, 12, 28),
        make_oracle("dp_storage", "two_row", "baseline", "full table versus two-row frontier", baseline1, globals_full),
        make_oracle("dp_storage", "two_row", "optimized", "full table versus two-row frontier", optimized1, globals_roll),
        ref1,
    ))

    baseline2 = """
int j = 0;
while (j < n) { table[0][j] = seed + j; table[1][j] = seed - j; j = j + 1; }
int i = 2;
while (i < n) {
  j = 0;
  while (j < n) {
    table[i][j] = table[i-1][j] + table[i-2][j] + (i+j) % 5;
    j = j + 1;
  }
  i = i + 1;
}
int acc = 0; j = 0;
while (j < n) { acc = checksum_step(acc, table[n-1][j], j); j = j + 1; }
putint(acc); putch(10); return 0;
"""
    optimized2 = """
int j = 0;
while (j < n) { row[0][j] = seed + j; row[1][j] = seed - j; j = j + 1; }
int i = 2;
while (i < n) {
  int cur = i % 3; int prev = (i+2) % 3; int older = (i+1) % 3; j = 0;
  while (j < n) {
    row[cur][j] = row[prev][j] + row[older][j] + (i+j) % 5;
    j = j + 1;
  }
  i = i + 1;
}
int acc = 0; j = 0;
while (j < n) { acc = checksum_step(acc, row[(n-1)%3][j], j); j = j + 1; }
putint(acc); putch(10); return 0;
"""
    def ref2(n: int, seed: int) -> int:
        table = [[0] * n for _ in range(n)]
        for j in range(n):
            table[0][j], table[1][j] = add32(seed, j), sub32(seed, j)
        for i in range(2, n):
            for j in range(n):
                table[i][j] = add32(
                    add32(table[i - 1][j], table[i - 2][j]), (i + j) % 5
                )
        return checksum_vector(table[-1])
    result.append(OracleSpec(
        "dp_storage", "three_row", "full history versus three-row ring", (4, 16, 40),
        make_oracle("dp_storage", "three_row", "baseline", "full history versus three-row ring", baseline2, globals_full),
        make_oracle("dp_storage", "three_row", "optimized", "full history versus three-row ring", optimized2, globals_roll),
        ref2,
    ))

    baseline3 = """
int i = 0;
while (i < n) { table[0][i] = seed + i; i = i + 1; }
i = 1;
while (i < n) {
  table[i][0] = seed - i; int j = 1;
  while (j < n) { table[i][j] = table[i-1][j-1] + i * 7 + j; j = j + 1; }
  i = i + 1;
}
int acc = 0; i = 0;
while (i < n) { acc = checksum_step(acc, table[n-1][i], i); i = i + 1; }
putint(acc); putch(10); return 0;
"""
    optimized3 = """
int j = 0;
while (j < n) { row[0][j] = seed + j; j = j + 1; }
int i = 1;
while (i < n) {
  j = n - 1;
  while (j >= 1) { row[0][j] = row[0][j-1] + i * 7 + j; j = j - 1; }
  row[0][0] = seed - i; i = i + 1;
}
int acc = 0; j = 0;
while (j < n) { acc = checksum_step(acc, row[0][j], j); j = j + 1; }
putint(acc); putch(10); return 0;
"""
    def ref3(n: int, seed: int) -> int:
        row = [add32(seed, j) for j in range(n)]
        for i in range(1, n):
            old = row[:]
            row[0] = sub32(seed, i)
            for j in range(1, n):
                row[j] = add32(old[j - 1], i * 7 + j)
        return checksum_vector(row)
    result.append(OracleSpec(
        "dp_storage", "reverse_single_row", "diagonal dependence contracted in reverse order", (4, 20, 56),
        make_oracle("dp_storage", "reverse_single_row", "baseline", "diagonal dependence contracted in reverse order", baseline3, globals_full),
        make_oracle("dp_storage", "reverse_single_row", "optimized", "diagonal dependence contracted in reverse order", optimized3, globals_roll),
        ref3,
    ))
    return result


def oracle_prefix_scan() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    globals_text = "int data[8192]; int output_data[8192];"
    variants = (
        ("forward_prefix", "overlapping forward prefixes", False, False),
        ("reverse_suffix", "overlapping reverse suffixes", True, False),
        ("weighted_prefix", "overlapping weighted prefixes", False, True),
    )
    for variant, desc, reverse, weighted in variants:
        init = "int i = 0; while (i < n) { data[i] = next_small(); output_data[i] = 0; i = i + 1; }\n"
        if reverse:
            baseline_compute = """
i = 0;
while (i < n) {
  int j = i; int sum = 0;
  while (j < n) { sum = sum + data[j]; j = j + 1; }
  output_data[i] = sum; i = i + 1;
}
"""
            optimized_compute = """
int running = 0; i = n - 1;
while (i >= 0) { running = running + data[i]; output_data[i] = running; i = i - 1; }
"""
        elif weighted:
            baseline_compute = """
i = 0;
while (i < n) {
  int j = 0; int sum = 0;
  while (j <= i) { sum = sum + data[j] * (j + 1); j = j + 1; }
  output_data[i] = sum; i = i + 1;
}
"""
            optimized_compute = """
int running = 0; i = 0;
while (i < n) {
  running = running + data[i] * (i + 1); output_data[i] = running; i = i + 1;
}
"""
        else:
            baseline_compute = """
i = 0;
while (i < n) {
  int j = 0; int sum = 0;
  while (j <= i) { sum = sum + data[j]; j = j + 1; }
  output_data[i] = sum; i = i + 1;
}
"""
            optimized_compute = """
int running = 0; i = 0;
while (i < n) { running = running + data[i]; output_data[i] = running; i = i + 1; }
"""
        finish = """
int acc = 0; i = 0;
while (i < n) { acc = checksum_step(acc, output_data[i], i); i = i + 1; }
putint(acc); putch(10); return 0;
"""
        def make_ref(reverse: bool, weighted: bool) -> Callable[[int, int], int]:
            def reference(n: int, seed: int) -> int:
                rng = Rng(seed)
                data = vector(n, rng)
                out = [0] * n
                if reverse:
                    running = 0
                    for i in range(n - 1, -1, -1):
                        running = add32(running, data[i])
                        out[i] = running
                else:
                    running = 0
                    for i in range(n):
                        term = mul32(data[i], i + 1) if weighted else data[i]
                        running = add32(running, term)
                        out[i] = running
                return checksum_vector(out)
            return reference
        result.append(OracleSpec(
            "prefix_scan", variant, desc, (16, 256, 2048),
            make_oracle("prefix_scan", variant, "baseline", desc, init + baseline_compute + finish, globals_text),
            make_oracle("prefix_scan", variant, "optimized", desc, init + optimized_compute + finish, globals_text),
            make_ref(reverse, weighted),
        ))
    return result


def oracle_linear_transition() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    baseline1 = """
int x = seed; int i = 0;
while (i < n) { x = 3 * x + 5; i = i + 1; }
putint(x); putch(10); return 0;
"""
    optimized1 = """
int result_mul = 1; int result_add = 0;
int base_mul = 3; int base_add = 5; int k = n;
while (k != 0) {
  if (k % 2 != 0) {
    result_add = base_mul * result_add + base_add;
    result_mul = base_mul * result_mul;
  }
  base_add = base_mul * base_add + base_add;
  base_mul = base_mul * base_mul;
  k = k / 2;
}
int x = result_mul * seed + result_add;
putint(x); putch(10); return 0;
"""
    def ref_affine(n: int, seed: int) -> int:
        x = seed
        for _ in range(n):
            x = add32(mul32(3, x), 5)
        return x
    result.append(OracleSpec(
        "linear_transition", "affine_scalar", "scalar affine transition exponentiation", (8, 1000, 1000000),
        make_oracle("linear_transition", "affine_scalar", "baseline", "scalar affine transition exponentiation", baseline1),
        make_oracle("linear_transition", "affine_scalar", "optimized", "scalar affine transition exponentiation", optimized1),
        ref_affine,
    ))

    baseline2 = """
int x = seed; int y = seed + 1; int i = 0;
while (i < n) { int next = x + y; x = y; y = next; i = i + 1; }
int acc = checksum_step(x, y, n);
putint(acc); putch(10); return 0;
"""
    helpers2 = """
int m00; int m01; int m10; int m11;
void square_matrix() {
  int a00 = m00*m00 + m01*m10;
  int a01 = m00*m01 + m01*m11;
  int a10 = m10*m00 + m11*m10;
  int a11 = m10*m01 + m11*m11;
  m00=a00; m01=a01; m10=a10; m11=a11;
}
"""
    optimized2 = """
int r00=1; int r01=0; int r10=0; int r11=1;
m00=0; m01=1; m10=1; m11=1; int k=n;
while (k != 0) {
  if (k % 2 != 0) {
    int a00=r00*m00+r01*m10; int a01=r00*m01+r01*m11;
    int a10=r10*m00+r11*m10; int a11=r10*m01+r11*m11;
    r00=a00; r01=a01; r10=a10; r11=a11;
  }
  square_matrix(); k=k/2;
}
int x=r00*seed+r01*(seed+1); int y=r10*seed+r11*(seed+1);
int acc=checksum_step(x,y,n);
putint(acc); putch(10); return 0;
"""
    def ref_fib(n: int, seed: int) -> int:
        x, y = seed, add32(seed, 1)
        for _ in range(n):
            x, y = y, add32(x, y)
        return checksum_step(x, y, n)
    result.append(OracleSpec(
        "linear_transition", "fibonacci_2d", "two-dimensional linear transition", (8, 1000, 100000),
        make_oracle("linear_transition", "fibonacci_2d", "baseline", "two-dimensional linear transition", baseline2),
        make_oracle("linear_transition", "fibonacci_2d", "optimized", "two-dimensional linear transition", optimized2, helpers=helpers2),
        ref_fib,
    ))

    baseline3 = """
int x=seed; int y=seed+3; int i=0;
while (i<n) { int nx=2*x+y+1; int ny=x+3*y+2; x=nx; y=ny; i=i+1; }
int acc=checksum_step(x,y,n); putint(acc); putch(10); return 0;
"""
    globals3 = "int base_m[3][3]; int result_m[3][3]; int temp_m[3][3];"
    helpers3 = """
void multiply_result_base() {
  int i=0;
  while(i<3){int j=0;while(j<3){int k=0;int s=0;while(k<3){s=s+result_m[i][k]*base_m[k][j];k=k+1;}temp_m[i][j]=s;j=j+1;}i=i+1;}
  i=0;while(i<3){int j=0;while(j<3){result_m[i][j]=temp_m[i][j];j=j+1;}i=i+1;}
}
void square_base() {
  int i=0;
  while(i<3){int j=0;while(j<3){int k=0;int s=0;while(k<3){s=s+base_m[i][k]*base_m[k][j];k=k+1;}temp_m[i][j]=s;j=j+1;}i=i+1;}
  i=0;while(i<3){int j=0;while(j<3){base_m[i][j]=temp_m[i][j];j=j+1;}i=i+1;}
}
"""
    optimized3 = """
int i=0;while(i<3){int j=0;while(j<3){result_m[i][j]=0;base_m[i][j]=0;j=j+1;}result_m[i][i]=1;i=i+1;}
base_m[0][0]=2;base_m[0][1]=1;base_m[0][2]=1;
base_m[1][0]=1;base_m[1][1]=3;base_m[1][2]=2;base_m[2][2]=1;
int k=n;while(k!=0){if(k%2!=0){multiply_result_base();}square_base();k=k/2;}
int x=result_m[0][0]*seed+result_m[0][1]*(seed+3)+result_m[0][2];
int y=result_m[1][0]*seed+result_m[1][1]*(seed+3)+result_m[1][2];
int acc=checksum_step(x,y,n);putint(acc);putch(10);return 0;
"""
    def ref_affine2(n: int, seed: int) -> int:
        x, y = seed, add32(seed, 3)
        for _ in range(n):
            x, y = add32(add32(mul32(2, x), y), 1), add32(add32(x, mul32(3, y)), 2)
        return checksum_step(x, y, n)
    result.append(OracleSpec(
        "linear_transition", "affine_2d", "two-dimensional affine transition", (8, 1000, 100000),
        make_oracle("linear_transition", "affine_2d", "baseline", "two-dimensional affine transition", baseline3),
        make_oracle("linear_transition", "affine_2d", "optimized", "two-dimensional affine transition", optimized3, globals3, helpers3),
        ref_affine2,
    ))
    return result


def oracle_fusion() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    globals_text = "int a[8192]; int b[8192]; int tmp1[8192]; int tmp2[8192]; int out[8192];"
    variants = [
        (
            "single_temporary",
            "one elementwise producer temporary",
            """
int i=0;while(i<n){a[i]=next_small();b[i]=next_small();i=i+1;}
i=0;while(i<n){tmp1[i]=a[i]*3+1;i=i+1;}
i=0;while(i<n){out[i]=tmp1[i]*tmp1[i]+b[i];i=i+1;}
""",
            """
int i=0;while(i<n){a[i]=next_small();b[i]=next_small();i=i+1;}
i=0;while(i<n){int t=a[i]*3+1;out[i]=t*t+b[i];i=i+1;}
""",
            1,
        ),
        (
            "two_temporaries",
            "two elementwise producer temporaries",
            """
int i=0;while(i<n){a[i]=next_small();b[i]=next_small();i=i+1;}
i=0;while(i<n){tmp1[i]=a[i]*5-b[i];i=i+1;}
i=0;while(i<n){tmp2[i]=tmp1[i]+a[i]*2;i=i+1;}
i=0;while(i<n){out[i]=tmp2[i]*3+tmp1[i];i=i+1;}
""",
            """
int i=0;while(i<n){a[i]=next_small();b[i]=next_small();i=i+1;}
i=0;while(i<n){int t1=a[i]*5-b[i];int t2=t1+a[i]*2;out[i]=t2*3+t1;i=i+1;}
""",
            2,
        ),
        (
            "stencil_producer",
            "non-duplicating three-point producer contraction",
            """
int i=0;while(i<n){a[i]=next_small();b[i]=next_small();tmp1[i]=0;out[i]=0;i=i+1;}
i=1;while(i+1<n){tmp1[i]=a[i-1]+a[i]+a[i+1];i=i+1;}
i=1;while(i+1<n){out[i]=tmp1[i]*3+b[i];i=i+1;}
""",
            """
int i=0;while(i<n){a[i]=next_small();b[i]=next_small();tmp1[i]=0;out[i]=0;i=i+1;}
i=1;while(i+1<n){int t=a[i-1]+a[i]+a[i+1];out[i]=t*3+b[i];i=i+1;}
""",
            3,
        ),
    ]
    finish = """
int acc=0;int i2=0;while(i2<n){acc=checksum_step(acc,out[i2],i2);i2=i2+1;}
putint(acc);putch(10);return 0;
"""
    def make_ref(kind: int) -> Callable[[int, int], int]:
        def reference(n: int, seed: int) -> int:
            rng = Rng(seed)
            a = [0] * n
            b = [0] * n
            for i in range(n):
                a[i], b[i] = rng.next_small(), rng.next_small()
            out = [0] * n
            if kind == 1:
                for i in range(n):
                    t = add32(mul32(a[i], 3), 1)
                    out[i] = add32(mul32(t, t), b[i])
            elif kind == 2:
                for i in range(n):
                    t1 = sub32(mul32(a[i], 5), b[i])
                    t2 = add32(t1, mul32(a[i], 2))
                    out[i] = add32(mul32(t2, 3), t1)
            else:
                for i in range(1, n - 1):
                    t = add32(add32(a[i - 1], a[i]), a[i + 1])
                    out[i] = add32(mul32(t, 3), b[i])
            return checksum_vector(out)
        return reference
    for variant, desc, baseline, optimized, kind in variants:
        result.append(OracleSpec(
            "fusion", variant, desc, (16, 512, 4096),
            make_oracle("fusion", variant, "baseline", desc, baseline + finish, globals_text),
            make_oracle("fusion", variant, "optimized", desc, optimized + finish, globals_text),
            make_ref(kind),
        ))
    return result


def oracle_structured_kernel() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    globals_matrix = "int a[32][32]; int b[32][32]; int c[32][32]; int temp[32][32];"
    init_matrix = """
int i=0;while(i<n){int j=0;while(j<n){a[i][j]=next_small();b[i][j]=next_small();c[i][j]=0;temp[i][j]=0;j=j+1;}i=i+1;}
"""
    base_gemm = init_matrix + """
i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){c[i][j]=c[i][j]+a[i][k]*b[k][j];k=k+1;}j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,c[i][j],index);index=index+1;j=j+1;}i=i+1;}
putint(acc);putch(10);return 0;
"""
    opt_gemm = init_matrix + """
i=0;while(i<n){int k=0;while(k<n){int av=a[i][k];int j=0;while(j<n){c[i][j]=c[i][j]+av*b[k][j];j=j+1;}k=k+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,c[i][j],index);index=index+1;j=j+1;}i=i+1;}
putint(acc);putch(10);return 0;
"""
    def ref_gemm(n: int, seed: int) -> int:
        rng = Rng(seed)
        a = [[0] * n for _ in range(n)]
        b = [[0] * n for _ in range(n)]
        c = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                a[i][j], b[i][j] = rng.next_small(), rng.next_small()
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    c[i][j] = add32(c[i][j], mul32(a[i][k], b[k][j]))
        return checksum_matrix(c)
    result.append(OracleSpec(
        "structured_kernel", "gemm_loop_order", "GEMM ijk versus ikj scalar schedule", (4, 12, 24),
        make_oracle("structured_kernel", "gemm_loop_order", "baseline", "GEMM ijk versus ikj scalar schedule", base_gemm, globals_matrix),
        make_oracle("structured_kernel", "gemm_loop_order", "optimized", "GEMM ijk versus ikj scalar schedule", opt_gemm, globals_matrix),
        ref_gemm,
    ))

    base_transpose = init_matrix + """
i=0;while(i<n){int j=0;while(j<n){temp[j][i]=a[i][j];j=j+1;}i=i+1;}
int acc=0;i=0;while(i<n){int sum=0;int j=0;while(j<n){sum=sum+temp[i][j];j=j+1;}acc=checksum_step(acc,sum,i);i=i+1;}
putint(acc);putch(10);return 0;
"""
    opt_transpose = init_matrix + """
int acc=0;int j=0;while(j<n){int sum=0;i=0;while(i<n){sum=sum+a[i][j];i=i+1;}acc=checksum_step(acc,sum,j);j=j+1;}
putint(acc);putch(10);return 0;
"""
    def ref_transpose(n: int, seed: int) -> int:
        rng = Rng(seed)
        a = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                a[i][j], _ = rng.next_small(), rng.next_small()
        acc = 0
        for j in range(n):
            total = 0
            for i in range(n):
                total = add32(total, a[i][j])
            acc = checksum_step(acc, total, j)
        return acc
    result.append(OracleSpec(
        "structured_kernel", "transpose_reduction", "transpose temporary fused into column reduction", (4, 16, 32),
        make_oracle("structured_kernel", "transpose_reduction", "baseline", "transpose temporary fused into column reduction", base_transpose, globals_matrix),
        make_oracle("structured_kernel", "transpose_reduction", "optimized", "transpose temporary fused into column reduction", opt_transpose, globals_matrix),
        ref_transpose,
    ))

    init_stencil = """
int i=0;while(i<n){int j=0;while(j<n){a[i][j]=next_small()+32;c[i][j]=0;j=j+1;}i=i+1;}
"""
    base_stencil = init_stencil + """
i=0;while(i<n){int j=0;while(j<n){if(i==0||j==0||i+1==n||j+1==n){c[i][j]=a[i][j];}else{c[i][j]=a[i][j]*2+a[i-1][j]+a[i+1][j]+a[i][j-1]+a[i][j+1];}j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,c[i][j],index);index=index+1;j=j+1;}i=i+1;}putint(acc);putch(10);return 0;
"""
    opt_stencil = init_stencil + """
i=0;while(i<n){int j=0;while(j<n){c[i][j]=a[i][j];j=j+1;}i=i+1;}
i=1;while(i+1<n){int j=1;while(j+1<n){int center=a[i][j];int vertical=a[i-1][j]+a[i+1][j];int horizontal=a[i][j-1]+a[i][j+1];c[i][j]=center*2+vertical+horizontal;j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,c[i][j],index);index=index+1;j=j+1;}i=i+1;}putint(acc);putch(10);return 0;
"""
    def ref_stencil_kernel(n: int, seed: int) -> int:
        rng = Rng(seed)
        values = [[rng.next_small() + 32 for _ in range(n)] for _ in range(n)]
        out = [row[:] for row in values]
        for i in range(1, n - 1):
            for j in range(1, n - 1):
                out[i][j] = add32(
                    add32(mul32(values[i][j], 2), add32(values[i - 1][j], values[i + 1][j])),
                    add32(values[i][j - 1], values[i][j + 1]),
                )
        return checksum_matrix(out)
    result.append(OracleSpec(
        "structured_kernel", "stencil_boundary_split", "branching stencil versus boundary/interior split schedule", (6, 16, 32),
        make_oracle("structured_kernel", "stencil_boundary_split", "baseline", "branching stencil versus boundary/interior split schedule", base_stencil, globals_matrix),
        make_oracle("structured_kernel", "stencil_boundary_split", "optimized", "branching stencil versus boundary/interior split schedule", opt_stencil, globals_matrix),
        ref_stencil_kernel,
    ))
    return result


def oracle_memoization() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    base_fib_helpers = """
int fib(int x){if(x<2){return x;}return fib(x-1)+fib(x-2);}
"""
    opt_fib_globals = "int memo[64]; int seen[64];"
    opt_fib_helpers = """
int fib(int x){if(x<2){return x;}if(seen[x]!=0){return memo[x];}int value=fib(x-1)+fib(x-2);seen[x]=1;memo[x]=value;return value;}
"""
    body_fib = "int value=fib(n)+seed;putint(value);putch(10);return 0;"
    def ref_fib(n: int, seed: int) -> int:
        a, b = 0, 1
        for _ in range(n):
            a, b = b, add32(a, b)
        return add32(a, seed)
    result.append(OracleSpec(
        "memoization", "fibonacci", "overlapping scalar recursion", (8, 20, 30),
        make_oracle("memoization", "fibonacci", "baseline", "overlapping scalar recursion", body_fib, helpers=base_fib_helpers),
        make_oracle("memoization", "fibonacci", "optimized", "overlapping scalar recursion", body_fib, opt_fib_globals, opt_fib_helpers),
        ref_fib,
    ))

    base_binom_helpers = """
int choose(int x,int k){if(k==0||k==x){return 1;}return choose(x-1,k-1)+choose(x-1,k);}
"""
    opt_binom_globals = "int memo[32][32]; int seen[32][32];"
    opt_binom_helpers = """
int choose(int x,int k){if(k==0||k==x){return 1;}if(seen[x][k]!=0){return memo[x][k];}int value=choose(x-1,k-1)+choose(x-1,k);seen[x][k]=1;memo[x][k]=value;return value;}
"""
    body_binom = "int value=choose(n,n/2)+seed;putint(value);putch(10);return 0;"
    def ref_binom(n: int, seed: int) -> int:
        row = [0] * (n + 1)
        row[0] = 1
        for i in range(1, n + 1):
            for k in range(i, 0, -1):
                row[k] = add32(row[k], row[k - 1])
        return add32(row[n // 2], seed)
    result.append(OracleSpec(
        "memoization", "binomial", "two-parameter overlapping recursion", (6, 12, 18),
        make_oracle("memoization", "binomial", "baseline", "two-parameter overlapping recursion", body_binom, helpers=base_binom_helpers),
        make_oracle("memoization", "binomial", "optimized", "two-parameter overlapping recursion", body_binom, opt_binom_globals, opt_binom_helpers),
        ref_binom,
    ))

    base_grid_helpers = """
int paths(int x,int y){if(x==0||y==0){return 1;}return paths(x-1,y)+paths(x,y-1);}
"""
    opt_grid_globals = "int memo[24][24]; int seen[24][24];"
    opt_grid_helpers = """
int paths(int x,int y){if(x==0||y==0){return 1;}if(seen[x][y]!=0){return memo[x][y];}int value=paths(x-1,y)+paths(x,y-1);seen[x][y]=1;memo[x][y]=value;return value;}
"""
    body_grid = "int value=paths(n,n/2)+seed;putint(value);putch(10);return 0;"
    def ref_grid(n: int, seed: int) -> int:
        rows, cols = n + 1, n // 2 + 1
        dp = [[1] * cols for _ in range(rows)]
        for i in range(1, rows):
            for j in range(1, cols):
                dp[i][j] = add32(dp[i - 1][j], dp[i][j - 1])
        return add32(dp[-1][-1], seed)
    result.append(OracleSpec(
        "memoization", "grid_paths", "two-dimensional grid-state recursion", (4, 8, 12),
        make_oracle("memoization", "grid_paths", "baseline", "two-dimensional grid-state recursion", body_grid, helpers=base_grid_helpers),
        make_oracle("memoization", "grid_paths", "optimized", "two-dimensional grid-state recursion", body_grid, opt_grid_globals, opt_grid_helpers),
        ref_grid,
    ))
    return result


def oracle_bitset() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    globals_text = "int left_bits[30]; int right_bits[30]; int output_bits[30];"
    variants = (
        ("boolean_or", "packed boolean union", 0),
        ("boolean_and", "packed boolean intersection", 1),
        ("boolean_xor", "packed boolean symmetric difference", 2),
    )
    for variant, desc, op in variants:
        init = """
int i=0;while(i<n){int x=next_small();if(x<0){x=0-x;}left_bits[i]=x%2;x=next_small();if(x<0){x=0-x;}right_bits[i]=x%2;output_bits[i]=0;i=i+1;}
"""
        if op == 0:
            calc = "if(left_bits[i]!=0||right_bits[i]!=0){output_bits[i]=1;}"
            packed_calc = "int bit=(left_word/power)%2+(right_word/power)%2;if(bit!=0){output_word=output_word+power;}"
        elif op == 1:
            calc = "if(left_bits[i]!=0&&right_bits[i]!=0){output_bits[i]=1;}"
            packed_calc = "int bit=(left_word/power)%2*(right_word/power)%2;if(bit!=0){output_word=output_word+power;}"
        else:
            calc = "if(left_bits[i]!=right_bits[i]){output_bits[i]=1;}"
            packed_calc = "int bit=((left_word/power)%2+(right_word/power)%2)%2;if(bit!=0){output_word=output_word+power;}"
        baseline = init + f"""
i=0;while(i<n){{{calc}i=i+1;}}
int acc=0;i=0;while(i<n){{acc=checksum_step(acc,output_bits[i],i);i=i+1;}}
putint(acc);putch(10);return 0;
"""
        optimized = init + f"""
int left_word=0;int right_word=0;int power=1;i=0;
while(i<n){{left_word=left_word+left_bits[i]*power;right_word=right_word+right_bits[i]*power;power=power*2;i=i+1;}}
int output_word=0;power=1;i=0;
while(i<n){{{packed_calc}power=power*2;i=i+1;}}
int acc=0;power=1;i=0;while(i<n){{int bit=(output_word/power)%2;acc=checksum_step(acc,bit,i);power=power*2;i=i+1;}}
putint(acc);putch(10);return 0;
"""
        def make_ref(op: int) -> Callable[[int, int], int]:
            def reference(n: int, seed: int) -> int:
                rng = Rng(seed)
                out = [0] * n
                for i in range(n):
                    lhs = abs(rng.next_small()) % 2
                    rhs = abs(rng.next_small()) % 2
                    if op == 0:
                        out[i] = int(bool(lhs or rhs))
                    elif op == 1:
                        out[i] = int(bool(lhs and rhs))
                    else:
                        out[i] = int(lhs != rhs)
                return checksum_vector(out)
            return reference
        result.append(OracleSpec(
            "bitset", variant, desc, (8, 16, 28),
            make_oracle("bitset", variant, "baseline", desc, baseline, globals_text),
            make_oracle("bitset", variant, "optimized", desc, optimized, globals_text),
            make_ref(op),
        ))
    return result


def oracle_finite_state() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    transitions = (
        ("affine_mod97", "affine transition over 97 states", 97, "return (state*5+1)%97;"),
        ("quadratic_mod31", "nonlinear transition over 31 states", 31, "return (state*state+3)%31;"),
        ("branch_mod53", "branching transition over 53 states", 53, "if(state%3==0){return (state+17)%53;}return (state*2+7)%53;"),
    )
    for variant, desc, states, transition_body in transitions:
        helpers = f"int transition(int state){{{transition_body}}}\n"
        baseline = f"""
int state=seed%{states};int i=0;while(i<n){{state=transition(state);i=i+1;}}
putint(state);putch(10);return 0;
"""
        optimized = f"""
int s=0;while(s<{states}){{jump_table[0][s]=transition(s);s=s+1;}}
int level=1;while(level<31){{s=0;while(s<{states}){{jump_table[level][s]=jump_table[level-1][jump_table[level-1][s]];s=s+1;}}level=level+1;}}
int state=seed%{states};int k=n;level=0;
while(k!=0){{if(k%2!=0){{state=jump_table[level][state];}}k=k/2;level=level+1;}}
putint(state);putch(10);return 0;
"""
        def make_ref(states: int, variant: str) -> Callable[[int, int], int]:
            def transition(state: int) -> int:
                if variant == "affine_mod97":
                    return (state * 5 + 1) % states
                if variant == "quadratic_mod31":
                    return (state * state + 3) % states
                if state % 3 == 0:
                    return (state + 17) % states
                return (state * 2 + 7) % states
            def reference(n: int, seed: int) -> int:
                state = seed % states
                for _ in range(n):
                    state = transition(state)
                return state
            return reference
        result.append(OracleSpec(
            "finite_state", variant, desc, (10, 10000, 1000000),
            make_oracle("finite_state", variant, "baseline", desc, baseline, helpers=helpers),
            make_oracle("finite_state", variant, "optimized", desc, optimized, "int jump_table[31][128];", helpers),
            make_ref(states, variant),
        ))
    return result


def oracle_recursion_worklist() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    tail_helpers = "int tail_step(int remaining,int value){if(remaining==0){return value;}return tail_step(remaining-1,value*3+1);}"
    baseline1 = "int value=tail_step(n,seed);putint(value);putch(10);return 0;"
    optimized1 = "int value=seed;int i=0;while(i<n){value=value*3+1;i=i+1;}putint(value);putch(10);return 0;"
    def ref_tail(n: int, seed: int) -> int:
        value = seed
        for _ in range(n):
            value = add32(mul32(value, 3), 1)
        return value
    result.append(OracleSpec(
        "recursion_worklist", "tail_to_loop", "tail recursion eliminated to a loop", (8, 64, 512),
        make_oracle("recursion_worklist", "tail_to_loop", "baseline", "tail recursion eliminated to a loop", baseline1, helpers=tail_helpers),
        make_oracle("recursion_worklist", "tail_to_loop", "optimized", "tail recursion eliminated to a loop", optimized1),
        ref_tail,
    ))

    range_helpers = "int sum_range(int lo,int hi,int bias){if(lo>=hi){return 0;}if(lo+1==hi){return lo*lo+bias;}int mid=lo+(hi-lo)/2;return sum_range(lo,mid,bias)+sum_range(mid,hi,bias);}"
    baseline2 = "int value=sum_range(0,n,seed%7);putint(value);putch(10);return 0;"
    optimized2 = "int value=0;int i=0;int bias=seed%7;while(i<n){value=value+i*i+bias;i=i+1;}putint(value);putch(10);return 0;"
    def ref_range(n: int, seed: int) -> int:
        value = 0
        for i in range(n):
            value = add32(value, add32(mul32(i, i), seed % 7))
        return value
    result.append(OracleSpec(
        "recursion_worklist", "divide_conquer_sum", "divide-and-conquer recursion flattened to iteration", (8, 128, 1024),
        make_oracle("recursion_worklist", "divide_conquer_sum", "baseline", "divide-and-conquer recursion flattened to iteration", baseline2, helpers=range_helpers),
        make_oracle("recursion_worklist", "divide_conquer_sum", "optimized", "divide-and-conquer recursion flattened to iteration", optimized2),
        ref_range,
    ))

    dfs_globals = "int visit_acc; int visit_index; int stack_nodes[131072]; int stack_depth[131072];"
    dfs_helpers = """
void visit_tree(int node,int depth,int limit){
  if(depth>=limit){return;}
  visit_acc=checksum_step(visit_acc,node+seed_global,visit_index);visit_index=visit_index+1;
  visit_tree(node*2+1,depth+1,limit);visit_tree(node*2+2,depth+1,limit);
}
int seed_global;
"""
    # seed_global must be declared before the helper for SysY name resolution.
    dfs_helpers = dfs_helpers.replace("int seed_global;\n", "")
    dfs_globals += " int seed_global;"
    baseline3 = "seed_global=seed;visit_acc=0;visit_index=0;visit_tree(0,0,n);putint(visit_acc);putch(10);return 0;"
    optimized3 = """
int top=0;stack_nodes[top]=0;stack_depth[top]=0;top=top+1;int acc=0;int index=0;
while(top>0){top=top-1;int node=stack_nodes[top];int depth=stack_depth[top];
  if(depth<n){acc=checksum_step(acc,node+seed,index);index=index+1;
    stack_nodes[top]=node*2+2;stack_depth[top]=depth+1;top=top+1;
    stack_nodes[top]=node*2+1;stack_depth[top]=depth+1;top=top+1;
  }
}
putint(acc);putch(10);return 0;
"""
    def ref_dfs(n: int, seed: int) -> int:
        acc = 0
        index = 0
        stack = [(0, 0)]
        while stack:
            node, depth = stack.pop()
            if depth < n:
                acc = checksum_step(acc, add32(node, seed), index)
                index += 1
                stack.append((node * 2 + 2, depth + 1))
                stack.append((node * 2 + 1, depth + 1))
        return acc
    result.append(OracleSpec(
        "recursion_worklist", "dfs_stack", "recursive preorder traversal versus explicit stack", (4, 10, 16),
        make_oracle("recursion_worklist", "dfs_stack", "baseline", "recursive preorder traversal versus explicit stack", baseline3, dfs_globals, dfs_helpers),
        make_oracle("recursion_worklist", "dfs_stack", "optimized", "recursive preorder traversal versus explicit stack", optimized3, dfs_globals),
        ref_dfs,
    ))
    return result




def oracle_boom_ilp() -> list[OracleSpec]:
    result: list[OracleSpec] = []
    globals_text = "int data_a[8192]; int data_b[8192];"
    init_reduction = "int i=0;while(i<n){data_a[i]=next_small()*next_small();i=i+1;}\n"
    baseline1 = init_reduction + """
int sum=0;i=0;while(i<n){sum=sum+data_a[i];i=i+1;}
putint(sum);putch(10);return 0;
"""
    optimized1 = init_reduction + """
int s0=0;int s1=0;int s2=0;int s3=0;i=0;
while(i+3<n){s0=s0+data_a[i];s1=s1+data_a[i+1];s2=s2+data_a[i+2];s3=s3+data_a[i+3];i=i+4;}
int sum=(s0+s1)+(s2+s3);while(i<n){sum=sum+data_a[i];i=i+1;}
putint(sum);putch(10);return 0;
"""
    def ref_reduction(n: int, seed: int) -> int:
        rng = Rng(seed)
        total = 0
        for _ in range(n):
            total = add32(total, mul32(rng.next_small(), rng.next_small()))
        return total
    result.append(OracleSpec(
        "boom_ilp", "reduction_multi_acc", "single reduction chain versus four accumulators", (32, 1024, 8192),
        make_oracle("boom_ilp", "reduction_multi_acc", "baseline", "single reduction chain versus four accumulators", baseline1, globals_text),
        make_oracle("boom_ilp", "reduction_multi_acc", "optimized", "single reduction chain versus four accumulators", optimized1, globals_text),
        ref_reduction,
    ))

    init_dot = "int i=0;while(i<n){data_a[i]=next_small();data_b[i]=next_small();i=i+1;}\n"
    baseline2 = init_dot + """
int dot=0;i=0;while(i<n){dot=dot+data_a[i]*data_b[i];i=i+1;}
putint(dot);putch(10);return 0;
"""
    optimized2 = init_dot + """
int d0=0;int d1=0;int d2=0;int d3=0;i=0;
while(i+3<n){d0=d0+data_a[i]*data_b[i];d1=d1+data_a[i+1]*data_b[i+1];d2=d2+data_a[i+2]*data_b[i+2];d3=d3+data_a[i+3]*data_b[i+3];i=i+4;}
int dot=(d0+d1)+(d2+d3);while(i<n){dot=dot+data_a[i]*data_b[i];i=i+1;}
putint(dot);putch(10);return 0;
"""
    def ref_dot(n: int, seed: int) -> int:
        rng = Rng(seed)
        total = 0
        for _ in range(n):
            total = add32(total, mul32(rng.next_small(), rng.next_small()))
        return total
    result.append(OracleSpec(
        "boom_ilp", "dot_unroll4", "scalar dot product versus four-way unrolled dot product", (32, 1024, 8192),
        make_oracle("boom_ilp", "dot_unroll4", "baseline", "scalar dot product versus four-way unrolled dot product", baseline2, globals_text),
        make_oracle("boom_ilp", "dot_unroll4", "optimized", "scalar dot product versus four-way unrolled dot product", optimized2, globals_text),
        ref_dot,
    ))

    baseline3 = init_dot + """
int x0=seed;int x1=seed+1;int x2=seed+2;int x3=seed+3;
i=0;while(i<n){x0=x0*3+data_a[i];i=i+1;}
i=0;while(i<n){x1=x1*5+data_b[i];i=i+1;}
i=0;while(i<n){x2=x2*7+data_a[i]-data_b[i];i=i+1;}
i=0;while(i<n){x3=x3*11+data_a[i]+data_b[i];i=i+1;}
int acc=checksum_step(checksum_step(x0,x1,n),checksum_step(x2,x3,n+1),n+2);
putint(acc);putch(10);return 0;
"""
    optimized3 = init_dot + """
int x0=seed;int x1=seed+1;int x2=seed+2;int x3=seed+3;
i=0;while(i<n){int av=data_a[i];int bv=data_b[i];x0=x0*3+av;x1=x1*5+bv;x2=x2*7+av-bv;x3=x3*11+av+bv;i=i+1;}
int acc=checksum_step(checksum_step(x0,x1,n),checksum_step(x2,x3,n+1),n+2);
putint(acc);putch(10);return 0;
"""
    def ref_chains(n: int, seed: int) -> int:
        rng = Rng(seed)
        a, b = [], []
        for _ in range(n):
            a.append(rng.next_small())
            b.append(rng.next_small())
        x0, x1, x2, x3 = seed, add32(seed, 1), add32(seed, 2), add32(seed, 3)
        for i in range(n):
            x0 = add32(mul32(x0, 3), a[i])
            x1 = add32(mul32(x1, 5), b[i])
            x2 = sub32(add32(mul32(x2, 7), a[i]), b[i])
            x3 = add32(add32(mul32(x3, 11), a[i]), b[i])
        return checksum_step(checksum_step(x0, x1, n), checksum_step(x2, x3, n + 1), n + 2)
    result.append(OracleSpec(
        "boom_ilp", "independent_chains", "four independent dependency chains scheduled in one loop", (32, 1024, 8192),
        make_oracle("boom_ilp", "independent_chains", "baseline", "four independent dependency chains scheduled in one loop", baseline3, globals_text),
        make_oracle("boom_ilp", "independent_chains", "optimized", "four independent dependency chains scheduled in one loop", optimized3, globals_text),
        ref_chains,
    ))
    return result


def all_oracles() -> tuple[OracleSpec, ...]:
    groups = (
        oracle_closed_form(),
        oracle_dp_storage(),
        oracle_prefix_scan(),
        oracle_linear_transition(),
        oracle_fusion(),
        oracle_structured_kernel(),
        oracle_memoization(),
        oracle_bitset(),
        oracle_finite_state(),
        oracle_recursion_worklist(),
        oracle_boom_ilp(),
    )
    return tuple(spec for group in groups for spec in group)


def generate_oracles(check: bool, failures: list[str]) -> list[dict[str, object]]:
    by_family: dict[str, list[dict[str, object]]] = {}
    tiers = ("small", "medium", "large")
    for spec in all_oracles():
        pair_root = ROOT / "oracles" / spec.family / spec.variant
        emit(pair_root / "baseline.sy", spec.baseline, check, failures)
        emit(pair_root / "optimized.sy", spec.optimized, check, failures)
        datasets: list[dict[str, object]] = []
        for tier, n in zip(tiers, spec.sizes):
            seed = stable_seed(f"oracle:{spec.identifier}", tier)
            value = spec.reference(n, seed)
            emit(pair_root / f"{tier}.in", f"{n} {seed}\n", check, failures)
            emit(pair_root / f"{tier}.out", f"{value}\n0\n", check, failures)
            datasets.append({"tier": tier, "n": n, "seed": seed, "input": f"oracles/{spec.identifier}/{tier}.in", "output": f"oracles/{spec.identifier}/{tier}.out"})
        by_family.setdefault(spec.family, []).append(
            {
                "variant": spec.variant,
                "structure_variant": spec.description,
                "baseline": f"oracles/{spec.identifier}/baseline.sy",
                "optimized": f"oracles/{spec.identifier}/optimized.sy",
                "datasets": datasets,
            }
        )
    return [
        {"family": family, "variants": variants}
        for family, variants in by_family.items()
    ]


STRUCTURAL_TAXONOMIES: tuple[str, ...] = (
    "01_mm",
    "03_sort",
    "conv2d",
    "crc",
    "crypto",
    "fft",
    "h-1",
    "h-10",
    "h-4",
    "h-5",
    "h-8",
    "h-9",
    "huffman",
    "knapsack_naive",
    "many_mat_cal",
    "matmul",
    "optimization_scheduling",
    "shuffle",
    "sl",
    "transpose",
)

STRUCTURAL_VARIANT_KINDS: tuple[str, ...] = (
    "cfg_induction_equivalent",
    "small_deterministic",
    "large_different_deterministic",
)

STRUCTURAL_SIZES: dict[str, tuple[int, int, int]] = {
    "01_mm": (8, 4, 20),
    "03_sort": (32, 12, 160),
    "conv2d": (12, 6, 24),
    "crc": (32, 8, 192),
    "crypto": (32, 8, 192),
    "fft": (32, 8, 128),
    "h-1": (48, 12, 192),
    "h-10": (16, 6, 32),
    "h-4": (48, 12, 192),
    "h-5": (128, 32, 192),
    "h-8": (16, 6, 28),
    "h-9": (48, 12, 192),
    "huffman": (64, 16, 192),
    "knapsack_naive": (12, 6, 24),
    "many_mat_cal": (8, 4, 18),
    "matmul": (12, 6, 24),
    "optimization_scheduling": (32, 8, 96),
    "shuffle": (48, 12, 192),
    "sl": (48, 12, 192),
    "transpose": (16, 6, 28),
}

STRUCTURAL_GLOBALS = """
int data[256]; int aux[256]; int output_data[256];
int matrix_a[32][32]; int matrix_b[32][32]; int matrix_c[32][32]; int matrix_d[32][32];
int dp_table[64][256]; int active_nodes[512]; int node_weight[512];
"""

STRUCTURAL_HELPERS = """
int xor_small(int a,int b){
  int result=0;int power=1;int i=0;
  while(i<16){int bit=((a/power)%2+(b/power)%2)%2;result=result+bit*power;power=power*2;i=i+1;}
  return result;
}
"""


def structural_init_source(variant_kind: str) -> str:
    if variant_kind == "cfg_induction_equivalent":
        return """
int i=0;
while(i<n){
  int first=next_small();int second=next_small();
  if(i%2==0){data[i]=first;aux[i]=second;}
  else{data[i]=first+1;data[i]=data[i]-1;aux[i]=second-1;aux[i]=aux[i]+1;}
  output_data[i]=0;i=i+1;
}
"""
    if variant_kind == "small_deterministic":
        return """
int i=0;
while(i<n){data[i]=next_small();aux[i]=next_small();output_data[i]=0;i=i+1;}
"""
    return """
int i=n-1;
while(i>=0){data[i]=next_small();aux[i]=next_small();output_data[i]=0;i=i-1;}
"""


def structural_body(taxonomy: str) -> str:
    if taxonomy == "01_mm":
        return """
int i=0;while(i<n){int j=0;while(j<n){matrix_a[i][j]=data[(i+j)%n];matrix_b[i][j]=aux[(i*3+j)%n];matrix_c[i][j]=0;j=j+1;}i=i+1;}
i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){matrix_c[i][j]=matrix_c[i][j]+matrix_a[i][k]*matrix_b[k][j];k=k+1;}j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,matrix_c[i][j],index);index=index+1;j=j+1;}i=i+1;}
putint(acc);putch(10);return 0;
"""
    if taxonomy == "03_sort":
        return """
int i=1;while(i<n){int key=data[i];int payload=aux[i];int j=i-1;while(j>=0&&data[j]>key){data[j+1]=data[j];aux[j+1]=aux[j];j=j-1;}data[j+1]=key;aux[j+1]=payload;i=i+1;}
int acc=0;i=0;while(i<n){acc=checksum_step(acc,data[i],i);acc=checksum_step(acc,aux[i],i+n);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "conv2d":
        return """
int i=0;while(i<n){int j=0;while(j<n){matrix_a[i][j]=data[(i*5+j)%n]+aux[(i+j*3)%n];matrix_b[i][j]=0;j=j+1;}i=i+1;}
i=1;while(i+1<n){int j=1;while(j+1<n){matrix_b[i][j]=matrix_a[i-1][j]+matrix_a[i][j-1]+matrix_a[i][j]*2+matrix_a[i][j+1]+matrix_a[i+1][j];j=j+1;}i=i+1;}
int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,matrix_b[i][j],index);index=index+1;j=j+1;}i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "crc":
        return """
int crc=65535;int i=0;while(i<n){int value=data[i];if(value<0){value=0-value;}value=value%256;crc=xor_small(crc,value*256);int bit=0;while(bit<8){int high=crc/32768;crc=(crc%32768)*2;if(high!=0){crc=xor_small(crc,4129);}bit=bit+1;}i=i+1;}putint(crc);putch(10);return 0;
"""
    if taxonomy == "crypto":
        return """
int key=(seed%251)+1;int acc=0;int i=0;while(i<n){int value=data[i];if(value<0){value=0-value;}value=value%256;int round=0;while(round<6){value=xor_small(value,key);value=(value*29+37+round)%256;key=(key*17+i+round+1)%256;round=round+1;}acc=checksum_step(acc,value,i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "fft":
        return """
int i=0;while(i<n){output_data[i]=data[i];i=i+1;}int width=1;while(width<n){int base=0;while(base<n){i=0;while(i<width&&base+i+width<n){int left=output_data[base+i];int right=output_data[base+i+width];output_data[base+i]=left+right;output_data[base+i+width]=left-right;i=i+1;}base=base+width*2;}width=width*2;}int acc=0;i=0;while(i<n){acc=checksum_step(acc,output_data[i],i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "h-1":
        return """
int x=seed;int y=seed+1;int i=0;while(i<n){x=x*3+data[i];y=y*5+x+aux[i];output_data[i]=x+y;i=i+1;}int acc=0;i=0;while(i<n){acc=checksum_step(acc,output_data[i],i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "h-10":
        return """
int i=0;while(i<n){dp_table[i][0]=data[i];dp_table[0][i]=aux[i];i=i+1;}i=1;while(i<n){int j=1;while(j<n){dp_table[i][j]=dp_table[i-1][j]+dp_table[i][j-1]+(data[i]+aux[j])%7;j=j+1;}i=i+1;}putint(dp_table[n-1][n-1]);putch(10);return 0;
"""
    if taxonomy == "h-4":
        return """
int running=0;int i=0;while(i<n){running=running+data[i];output_data[i]=running+aux[i];i=i+1;}int acc=0;i=0;while(i<n){acc=checksum_step(acc,output_data[i],i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "h-5":
        return """
int state=seed%37;int acc=0;int i=0;while(i<n){int event=data[i%256];if(event<0){event=0-event;}event=event%5;if(state%3==0){state=(state+event+7)%37;}else{state=(state*3+event+1)%37;}acc=checksum_step(acc,state,i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "h-8":
        return """
int i=0;while(i<n){int j=0;while(j<n){matrix_a[i][j]=data[(i+j)%n]-aux[(i*2+j)%n];j=j+1;}i=i+1;}int acc=0;i=0;while(i<n){int sum=0;int j=0;while(j<n){sum=sum+matrix_a[i][j]*data[j];j=j+1;}acc=checksum_step(acc,sum,i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "h-9":
        return """
int i=1;while(i<n){int key=data[i];int j=i-1;while(j>=0&&data[j]>key){data[j+1]=data[j];j=j-1;}data[j+1]=key;i=i+1;}int acc=0;i=0;while(i<n){int target=aux[i];int lo=0;int hi=n-1;int found=-1;while(lo<=hi){int mid=lo+(hi-lo)/2;if(data[mid]==target){found=mid;lo=hi+1;}else{if(data[mid]<target){lo=mid+1;}else{hi=mid-1;}}}acc=checksum_step(acc,found,i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "huffman":
        return """
int nodes=0;int i=0;while(i<n){int value=data[i];if(value<0){value=0-value;}value=value%32;node_weight[value]=node_weight[value]+1;i=i+1;}i=0;while(i<32){if(node_weight[i]!=0){node_weight[nodes]=node_weight[i];active_nodes[nodes]=1;nodes=nodes+1;}i=i+1;}int total=nodes;int path=0;while(nodes>1){int first=-1;int second=-1;i=0;while(i<total){if(active_nodes[i]!=0){if(first<0||node_weight[i]<node_weight[first]){second=first;first=i;}else{if(second<0||node_weight[i]<node_weight[second]){second=i;}}}i=i+1;}int merged=node_weight[first]+node_weight[second];path=path+merged;active_nodes[first]=0;active_nodes[second]=0;node_weight[total]=merged;active_nodes[total]=1;total=total+1;nodes=nodes-1;}putint(path);putch(10);return 0;
"""
    if taxonomy == "knapsack_naive":
        return """
int capacity=n*2;int c=0;while(c<=capacity){output_data[c]=0;c=c+1;}int i=0;while(i<n){int weight=data[i];if(weight<0){weight=0-weight;}weight=weight%7+1;int value=aux[i];if(value<0){value=0-value;}value=value%31+1;c=capacity;while(c>=weight){int candidate=output_data[c-weight]+value;if(candidate>output_data[c]){output_data[c]=candidate;}c=c-1;}i=i+1;}putint(output_data[capacity]);putch(10);return 0;
"""
    if taxonomy == "many_mat_cal":
        return """
int i=0;while(i<n){int j=0;while(j<n){matrix_a[i][j]=data[(i+j)%n];matrix_b[i][j]=aux[(i*3+j)%n];matrix_c[i][j]=0;matrix_d[i][j]=0;j=j+1;}i=i+1;}i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){matrix_c[i][j]=matrix_c[i][j]+matrix_a[i][k]*matrix_b[k][j];k=k+1;}j=j+1;}i=i+1;}i=0;while(i<n){int j=0;while(j<n){int k=0;while(k<n){matrix_d[i][j]=matrix_d[i][j]+matrix_c[i][k]*matrix_a[k][j];k=k+1;}j=j+1;}i=i+1;}int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,matrix_d[i][j],index);index=index+1;j=j+1;}i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "matmul":
        return structural_body("01_mm")
    if taxonomy == "optimization_scheduling":
        return """
int i=0;while(i<n){int duration=data[i];if(duration<0){duration=0-duration;}data[i]=duration%17+1;int priority=aux[i];if(priority<0){priority=0-priority;}aux[i]=priority%31+1;i=i+1;}i=1;while(i<n){int d=data[i];int p=aux[i];int j=i-1;while(j>=0&&aux[j]*d<p*data[j]){data[j+1]=data[j];aux[j+1]=aux[j];j=j-1;}data[j+1]=d;aux[j+1]=p;i=i+1;}int elapsed=0;int score=0;i=0;while(i<n){elapsed=elapsed+data[i];score=score+elapsed*aux[i];i=i+1;}putint(score);putch(10);return 0;
"""
    if taxonomy == "shuffle":
        return """
int i=0;while(i<n){output_data[i]=i;i=i+1;}i=n-1;while(i>0){int value=aux[i];if(value<0){value=0-value;}int j=value%(i+1);int temp=output_data[i];output_data[i]=output_data[j];output_data[j]=temp;i=i-1;}int acc=0;i=0;while(i<n){acc=checksum_step(acc,output_data[i],i);i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "sl":
        return """
int i=0;while(i<n){aux[i]=(i*7+3)%n;i=i+1;}int node=seed%n;int acc=0;i=0;while(i<n){acc=checksum_step(acc,data[node],i);node=aux[node];i=i+1;}putint(acc);putch(10);return 0;
"""
    if taxonomy == "transpose":
        return """
int i=0;while(i<n){int j=0;while(j<n){matrix_a[i][j]=data[(i*5+j)%n]+aux[(i+j*3)%n];matrix_b[j][i]=matrix_a[i][j];j=j+1;}i=i+1;}int acc=0;int index=0;i=0;while(i<n){int j=0;while(j<n){acc=checksum_step(acc,matrix_b[i][j],index);index=index+1;j=j+1;}i=i+1;}putint(acc);putch(10);return 0;
"""
    raise ValueError(taxonomy)


def structural_source(taxonomy: str, variant_kind: str) -> str:
    description = (
        f"clean-room structural variant for taxonomy {taxonomy}; "
        f"variant_kind={variant_kind}; role=structural_variant"
    )
    return make_program(
        f"structural_{taxonomy}_{variant_kind}",
        description,
        STRUCTURAL_GLOBALS,
        "{\n" + structural_init_source(variant_kind) + "}\n" + structural_body(taxonomy),
        STRUCTURAL_HELPERS,
    )


def structural_initial_data(n: int, seed: int, variant_kind: str) -> tuple[list[int], list[int]]:
    rng = Rng(seed)
    data = [0] * max(n, 256 if n > 256 else n)
    aux = [0] * len(data)
    indices = range(n - 1, -1, -1) if variant_kind == "large_different_deterministic" else range(n)
    for i in indices:
        data[i], aux[i] = rng.next_small(), rng.next_small()
    return data[:n], aux[:n]


def structural_reference(taxonomy: str, variant_kind: str, n: int, seed: int) -> int:
    data, aux = structural_initial_data(n, seed, variant_kind)
    if taxonomy in ("01_mm", "matmul"):
        a = [[data[(i + j) % n] for j in range(n)] for i in range(n)]
        b = [[aux[(i * 3 + j) % n] for j in range(n)] for i in range(n)]
        c = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    c[i][j] = add32(c[i][j], mul32(a[i][k], b[k][j]))
        return checksum_matrix(c)
    if taxonomy == "03_sort":
        records = sorted(zip(data, aux), key=lambda item: item[0])
        acc = 0
        for i, (key, payload) in enumerate(records):
            acc = checksum_step(acc, key, i)
            acc = checksum_step(acc, payload, i + n)
        return acc
    if taxonomy == "conv2d":
        a = [[data[(i * 5 + j) % n] + aux[(i + j * 3) % n] for j in range(n)] for i in range(n)]
        b = [[0] * n for _ in range(n)]
        for i in range(1, n - 1):
            for j in range(1, n - 1):
                b[i][j] = add32(add32(add32(a[i - 1][j], a[i][j - 1]), mul32(a[i][j], 2)), add32(a[i][j + 1], a[i + 1][j]))
        return checksum_matrix(b)
    if taxonomy == "crc":
        crc = 65535
        for item in data:
            crc ^= (abs(item) % 256) * 256
            for _ in range(8):
                high = crc // 32768
                crc = (crc % 32768) * 2
                if high:
                    crc ^= 4129
        return crc
    if taxonomy == "crypto":
        key, acc = seed % 251 + 1, 0
        for i, item in enumerate(data):
            value = abs(item) % 256
            for round_index in range(6):
                value = ((value ^ key) * 29 + 37 + round_index) % 256
                key = (key * 17 + i + round_index + 1) % 256
            acc = checksum_step(acc, value, i)
        return acc
    if taxonomy == "fft":
        out = data[:]
        width = 1
        while width < n:
            for base in range(0, n, width * 2):
                for i in range(width):
                    if base + i + width < n:
                        lhs, rhs = out[base + i], out[base + i + width]
                        out[base + i] = add32(lhs, rhs)
                        out[base + i + width] = sub32(lhs, rhs)
            width *= 2
        return checksum_vector(out)
    if taxonomy == "h-1":
        x, y, out = seed, add32(seed, 1), []
        for i in range(n):
            x = add32(mul32(x, 3), data[i])
            y = add32(add32(mul32(y, 5), x), aux[i])
            out.append(add32(x, y))
        return checksum_vector(out)
    if taxonomy == "h-10":
        table = [[0] * n for _ in range(n)]
        for i in range(n):
            table[i][0], table[0][i] = data[i], aux[i]
        for i in range(1, n):
            for j in range(1, n):
                table[i][j] = add32(add32(table[i - 1][j], table[i][j - 1]), rem32(add32(data[i], aux[j]), 7))
        return table[-1][-1]
    if taxonomy == "h-4":
        running, out = 0, []
        for i in range(n):
            running = add32(running, data[i])
            out.append(add32(running, aux[i]))
        return checksum_vector(out)
    if taxonomy == "h-5":
        state, acc = seed % 37, 0
        for i in range(n):
            event = abs(data[i % len(data)]) % 5
            state = (state + event + 7) % 37 if state % 3 == 0 else (state * 3 + event + 1) % 37
            acc = checksum_step(acc, state, i)
        return acc
    if taxonomy == "h-8":
        acc = 0
        for i in range(n):
            total = 0
            for j in range(n):
                cell = sub32(data[(i + j) % n], aux[(i * 2 + j) % n])
                total = add32(total, mul32(cell, data[j]))
            acc = checksum_step(acc, total, i)
        return acc
    if taxonomy == "h-9":
        ordered = sorted(data)
        acc = 0
        for i, target in enumerate(aux):
            lo, hi, found = 0, n - 1, -1
            while lo <= hi:
                mid = lo + (hi - lo) // 2
                if ordered[mid] == target:
                    found, lo = mid, hi + 1
                elif ordered[mid] < target:
                    lo = mid + 1
                else:
                    hi = mid - 1
            acc = checksum_step(acc, found, i)
        return acc
    if taxonomy == "huffman":
        frequencies = [0] * 32
        for item in data:
            frequencies[abs(item) % 32] += 1
        weights = [value for value in frequencies if value]
        path = 0
        while len(weights) > 1:
            weights.sort()
            merged = weights.pop(0) + weights.pop(0)
            path += merged
            weights.append(merged)
        return path
    if taxonomy == "knapsack_naive":
        capacity = n * 2
        dp = [0] * (capacity + 1)
        for i in range(n):
            weight, value = abs(data[i]) % 7 + 1, abs(aux[i]) % 31 + 1
            for current in range(capacity, weight - 1, -1):
                dp[current] = max(dp[current], dp[current - weight] + value)
        return dp[capacity]
    if taxonomy == "many_mat_cal":
        a = [[data[(i + j) % n] for j in range(n)] for i in range(n)]
        b = [[aux[(i * 3 + j) % n] for j in range(n)] for i in range(n)]
        c = [[0] * n for _ in range(n)]
        d = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    c[i][j] = add32(c[i][j], mul32(a[i][k], b[k][j]))
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    d[i][j] = add32(d[i][j], mul32(c[i][k], a[k][j]))
        return checksum_matrix(d)
    if taxonomy == "optimization_scheduling":
        jobs = [(abs(data[i]) % 17 + 1, abs(aux[i]) % 31 + 1) for i in range(n)]
        for i in range(1, n):
            job = jobs[i]
            j = i - 1
            while j >= 0 and jobs[j][1] * job[0] < job[1] * jobs[j][0]:
                jobs[j + 1] = jobs[j]
                j -= 1
            jobs[j + 1] = job
        elapsed = score = 0
        for duration, priority in jobs:
            elapsed += duration
            score = add32(score, mul32(elapsed, priority))
        return score
    if taxonomy == "shuffle":
        out = list(range(n))
        for i in range(n - 1, 0, -1):
            j = abs(aux[i]) % (i + 1)
            out[i], out[j] = out[j], out[i]
        return checksum_vector(out)
    if taxonomy == "sl":
        links = [(i * 7 + 3) % n for i in range(n)]
        node, acc = seed % n, 0
        for i in range(n):
            acc = checksum_step(acc, data[node], i)
            node = links[node]
        return acc
    if taxonomy == "transpose":
        a = [[data[(i * 5 + j) % n] + aux[(i + j * 3) % n] for j in range(n)] for i in range(n)]
        b = [[a[j][i] for j in range(n)] for i in range(n)]
        return checksum_matrix(b)
    raise ValueError(taxonomy)


def generate_structural_variants(
    check: bool, failures: list[str]
) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for taxonomy in STRUCTURAL_TAXONOMIES:
        for variant_index, variant_kind in enumerate(STRUCTURAL_VARIANT_KINDS):
            n = STRUCTURAL_SIZES[taxonomy][variant_index]
            identifier = f"structural:{taxonomy}:{variant_kind}"
            seed = stable_structure_seed(identifier, "fixed")
            root = ROOT / "structural_variants" / taxonomy
            source_path = root / f"{variant_kind}.sy"
            input_path = root / f"{variant_kind}.in"
            output_path = root / f"{variant_kind}.out"
            emit(source_path, structural_source(taxonomy, variant_kind), check, failures)
            emit(input_path, f"{n} {seed}\n", check, failures)
            value = structural_reference(taxonomy, variant_kind, n, seed)
            emit(output_path, f"{value}\n0\n", check, failures)
            manifest.append(
                {
                    "family_taxonomy": taxonomy,
                    "role": "structural_variant",
                    "variant_kind": variant_kind,
                    "n": n,
                    "seed": seed,
                    "source": f"structural_variants/{taxonomy}/{variant_kind}.sy",
                    "input": f"structural_variants/{taxonomy}/{variant_kind}.in",
                    "output": f"structural_variants/{taxonomy}/{variant_kind}.out",
                    "spdx": "MIT",
                    "provenance": "ACCELA clean-room taxonomy variant; no official source, input, or fingerprint accessed",
                }
            )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify checked-in artifacts")
    args = parser.parse_args()
    failures = generate(args.check)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    if args.check:
        print(
            f"verified {len(BENCHMARKS)} benchmarks, "
            f"{len(all_oracles())} oracle pairs, and "
            f"{len(STRUCTURAL_TAXONOMIES) * len(STRUCTURAL_VARIANT_KINDS)} structural variants"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
