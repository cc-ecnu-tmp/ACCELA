from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


_IDENTIFIER_START = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
_IDENTIFIER_CONTINUE = _IDENTIFIER_START | frozenset(b"0123456789")
_DECIMAL_DIGITS = frozenset(b"0123456789")
_HEX_DIGITS = _DECIMAL_DIGITS | frozenset(b"abcdefABCDEF")
_WHITESPACE = frozenset(b" \t\r\n\v\f")
_SYSY_TWO_BYTE_TOKENS = frozenset({b"<=", b">=", b"==", b"!=", b"&&", b"||"})
_NON_SYSY_TWO_BYTE_TOKENS = frozenset(
    {
        b"++",
        b"--",
        b"+=",
        b"-=",
        b"*=",
        b"/=",
        b"%=",
        b"&=",
        b"|=",
        b"^=",
        b"<<",
        b">>",
        b"->",
        b"::",
    }
)
_SYSY_ONE_BYTE_TOKENS = frozenset(b"+-*/%<>!=;,()[]{}")

REFERENCE_SOURCE_ADAPTER_PATH = "tools/benchmark/reference_source.py"
REFERENCE_COMPILE_DRIVER_PATH = "scripts/reference-compile.sh"
REFERENCE_BUILTIN_HEADER_PATH = "tools/qemu/sysy-builtins.h"
REFERENCE_DOCKERFILE_PATH = "tools/benchmark/reference-toolchain.Dockerfile"
REFERENCE_LAUNCHER_CONTRACT = {
    "python_executable": "python3",
    "python_mode": "isolated",
    "python_argv_prefix": ["python3", "-I"],
    "python_version_source": "proxy_execution.python",
    "python_version_probe": "platform.python_version",
    "docker_candidate_order": ["native", "windows"],
    "docker_readiness_argv": ["version", "--format", "{{.Server.Version}}"],
    "docker_fallback_policy": "transport_unreachable_only",
    "docker_image_inspect_identity": "frozen_tag_to_exact_id",
    "docker_run_identity": "exact_image_id",
    "stderr_records": [
        "ACCELA_REFERENCE_PYTHON",
        "ACCELA_REFERENCE_DOCKER_CANDIDATE",
        "ACCELA_REFERENCE_DOCKER",
        "ACCELA_REFERENCE_COMMAND",
    ],
}
REFERENCE_COMMON_SEMANTICS = (
    "source-adapter:sysy-lexical-boundary-v1",
    "source-adapter:cxx17-keywords-v1",
    "source-adapter:int32-literals-v1",
    "source-adapter:binary32-literals-v1",
    "-fwrapv",
    "-fno-fast-math",
    "-ffp-contract=off",
    "-ffreestanding",
    "-fno-builtin",
)
REFERENCE_BASELINES = {
    "gcc_13_3_o2": {
        "profile_id": "gcc-13.3-o2",
        "frontend": "gcc",
        "tool": "riscv-gcc",
        "version": "13.3.0",
        "optimization": "-O2",
        "executable": "/usr/bin/riscv64-linux-gnu-g++-13",
        "package": "gcc-13-riscv64-linux-gnu=13.3.0-6ubuntu2~24.04.1cross1",
        "cxx_package": "g++-13-riscv64-linux-gnu=13.3.0-6ubuntu2~24.04.1cross1",
    },
    "clang_18_o3": {
        "profile_id": "clang-18-o3",
        "frontend": "clang",
        "tool": "clang",
        "version": "18.1.3",
        "optimization": "-O3",
        "executable": "/usr/bin/clang-18",
        "package": "clang-18=1:18.1.3-1ubuntu1",
    },
}
REFERENCE_FRONTEND_ARGV = {
    "gcc": (
        "/usr/bin/riscv64-linux-gnu-g++-13",
        "-march=rv64gc",
        "-mabi=lp64d",
        "-mcmodel=medany",
        "-O2",
        "-fwrapv",
        "-fno-fast-math",
        "-ffp-contract=off",
        "-ffreestanding",
        "-fno-builtin",
        "-fno-exceptions",
        "-fno-rtti",
        "-fno-threadsafe-statics",
        "-fno-use-cxa-atexit",
        "-nostdinc++",
        "-std=c++17",
        "-x",
        "c++",
        "-include",
        "/support/sysy-builtins.h",
        "-S",
        "/output/.accela-sysy-reference.cpp",
        "-o",
        "/output/{artifact_name}",
    ),
    "clang": (
        "/usr/bin/clang-18",
        "--target=riscv64-unknown-elf",
        "-march=rv64gc",
        "-mabi=lp64d",
        "-mcmodel=medany",
        "-O3",
        "-fwrapv",
        "-fno-fast-math",
        "-ffp-contract=off",
        "-ffreestanding",
        "-fno-builtin",
        "-fno-addrsig",
        "-fno-exceptions",
        "-fno-rtti",
        "-fno-threadsafe-statics",
        "-fno-use-cxa-atexit",
        "-nostdinc++",
        "-std=c++17",
        "-x",
        "c++",
        "-include",
        "/support/sysy-builtins.h",
        "-S",
        "/output/.accela-sysy-reference.cpp",
        "-o",
        "/output/{artifact_name}",
    ),
}

# SysY has a deliberately smaller keyword set than C++17.  A legal SysY
# identifier may therefore be a C++ keyword.  The adapter renames only that
# difference; shared SysY/C++ keywords retain their language meaning.
_SYSY_KEYWORDS = frozenset(
    {"break", "const", "continue", "else", "float", "if", "int", "return", "void", "while"}
)
_CXX17_KEYWORDS = frozenset(
    {
        "alignas",
        "alignof",
        "and",
        "and_eq",
        "asm",
        "auto",
        "bitand",
        "bitor",
        "bool",
        "break",
        "case",
        "catch",
        "char",
        "char16_t",
        "char32_t",
        "class",
        "compl",
        "const",
        "constexpr",
        "const_cast",
        "continue",
        "decltype",
        "default",
        "delete",
        "do",
        "double",
        "dynamic_cast",
        "else",
        "enum",
        "explicit",
        "export",
        "extern",
        "false",
        "float",
        "for",
        "friend",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "mutable",
        "namespace",
        "new",
        "noexcept",
        "not",
        "not_eq",
        "nullptr",
        "operator",
        "or",
        "or_eq",
        "private",
        "protected",
        "public",
        "register",
        "reinterpret_cast",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "static_assert",
        "static_cast",
        "struct",
        "switch",
        "template",
        "this",
        "thread_local",
        "throw",
        "true",
        "try",
        "typedef",
        "typeid",
        "typename",
        "union",
        "unsigned",
        "using",
        "virtual",
        "void",
        "volatile",
        "wchar_t",
        "while",
        "xor",
        "xor_eq",
    }
)
_CXX_ONLY_KEYWORDS = _CXX17_KEYWORDS - _SYSY_KEYWORDS


class ReferenceSourceError(ValueError):
    """A source cannot be translated without changing the SysY contract."""


@dataclass(frozen=True)
class AdaptedSource:
    payload: bytes
    identifier_prefix: str | None
    renamed_identifiers: tuple[str, ...]
    integer_literal_count: int
    float_literal_count: int


@dataclass(frozen=True)
class ReferenceFrontendContract:
    image_tag: str
    image_id: str
    source_adapter_sha256: str
    builtin_header_sha256: str
    compiler_argv_sha256: str
    compiler_argv: tuple[str, ...]


@dataclass(frozen=True)
class _Piece:
    kind: str
    payload: bytes
    offset: int


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _physical_support_hash(
    root: Path,
    frontends: dict[str, object],
    *,
    path_key: str,
    hash_key: str,
    expected_path: str,
) -> str:
    observed_path = frontends.get(path_key)
    observed_hash = frontends.get(hash_key)
    if observed_path != expected_path or not isinstance(observed_hash, str):
        raise ReferenceSourceError(f"reference contract has invalid {path_key}")
    if re.fullmatch(r"[0-9a-f]{64}", observed_hash) is None:
        raise ReferenceSourceError(f"reference contract has invalid {hash_key}")
    relative = Path(*expected_path.split("/"))
    physical = (root / relative).resolve(strict=True)
    try:
        physical.relative_to(root)
    except ValueError as exc:
        raise ReferenceSourceError("reference support path escapes the workspace") from exc
    if not physical.is_file() or hashlib.sha256(physical.read_bytes()).hexdigest() != observed_hash:
        raise ReferenceSourceError(f"reference support hash mismatch: {path_key}")
    return observed_hash


def load_reference_frontend_contract(
    *,
    root: Path,
    snapshot_path: Path,
    frontend: str,
    artifact_name: str,
) -> ReferenceFrontendContract:
    if frontend not in REFERENCE_FRONTEND_ARGV:
        raise ReferenceSourceError(f"unknown reference frontend: {frontend}")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}", artifact_name) is None:
        raise ReferenceSourceError("reference artifact name contains unsupported bytes")
    workspace = root.resolve(strict=True)
    if not workspace.is_dir():
        raise ReferenceSourceError("reference workspace is not a directory")
    try:
        snapshot = json.loads(snapshot_path.resolve(strict=True).read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceSourceError("toolchain snapshot is not valid UTF-8 JSON") from exc
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "accela-toolchain-snapshot.v1":
        raise ReferenceSourceError("toolchain snapshot has an unsupported schema")
    if snapshot.get("target") != {"isa": "rv64gc", "abi": "lp64d", "code_model": "medany"}:
        raise ReferenceSourceError("toolchain snapshot target is not RV64GC/LP64D/medany")
    frontends = snapshot.get("reference_frontends")
    if not isinstance(frontends, dict):
        raise ReferenceSourceError("toolchain snapshot lacks reference_frontends")
    if frontends.get("frontend_language") != "c++17" or frontends.get(
        "common_semantics"
    ) != list(REFERENCE_COMMON_SEMANTICS):
        raise ReferenceSourceError("reference frontend language semantics have drifted")
    if frontends.get("launcher_contract") != REFERENCE_LAUNCHER_CONTRACT:
        raise ReferenceSourceError("reference launcher contract has drifted")

    _physical_support_hash(
        workspace,
        frontends,
        path_key="compile_driver_path",
        hash_key="compile_driver_sha256",
        expected_path=REFERENCE_COMPILE_DRIVER_PATH,
    )
    source_adapter_sha256 = _physical_support_hash(
        workspace,
        frontends,
        path_key="source_adapter_path",
        hash_key="source_adapter_sha256",
        expected_path=REFERENCE_SOURCE_ADAPTER_PATH,
    )
    builtin_header_sha256 = _physical_support_hash(
        workspace,
        frontends,
        path_key="builtin_header_path",
        hash_key="builtin_header_sha256",
        expected_path=REFERENCE_BUILTIN_HEADER_PATH,
    )
    dockerfile_hash = frontends.get("dockerfile_sha256")
    dockerfile = (workspace / Path(*REFERENCE_DOCKERFILE_PATH.split("/"))).resolve(strict=True)
    if (
        not isinstance(dockerfile_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", dockerfile_hash) is None
        or not dockerfile.is_file()
        or hashlib.sha256(dockerfile.read_bytes()).hexdigest() != dockerfile_hash
    ):
        raise ReferenceSourceError("reference Dockerfile hash mismatch")

    image_tag = frontends.get("local_image_tag")
    image_id = frontends.get("local_image_id")
    if not isinstance(image_tag, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", image_tag
    ) is None:
        raise ReferenceSourceError("reference image tag is invalid")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ReferenceSourceError("reference image ID is invalid")

    baseline = next(
        item for item in REFERENCE_BASELINES.values() if item["frontend"] == frontend
    )
    observed = frontends.get(frontend)
    if not isinstance(observed, dict):
        raise ReferenceSourceError(f"toolchain snapshot lacks {frontend}")
    for field in ("version", "optimization", "executable", "package"):
        if observed.get(field) != baseline[field]:
            raise ReferenceSourceError(f"reference frontend identity drift: {frontend}/{field}")
    if "cxx_package" in baseline and observed.get("cxx_package") != baseline["cxx_package"]:
        raise ReferenceSourceError(f"reference frontend identity drift: {frontend}/cxx_package")
    expected_argv = REFERENCE_FRONTEND_ARGV[frontend]
    observed_argv = observed.get("compiler_argv")
    if observed_argv != list(expected_argv):
        raise ReferenceSourceError(f"reference frontend argv drift: {frontend}")
    compiler_argv_sha256 = _canonical_sha256({"argv": observed_argv})
    if observed.get("compiler_argv_sha256") != compiler_argv_sha256:
        raise ReferenceSourceError(f"reference frontend argv hash mismatch: {frontend}")
    placeholders = sum(argument.count("{artifact_name}") for argument in expected_argv)
    if placeholders != 1:
        raise AssertionError("reference frontend argv must have exactly one artifact placeholder")
    rendered = tuple(
        argument.replace("{artifact_name}", artifact_name) for argument in expected_argv
    )
    if any("\n" in argument or "\r" in argument or not argument for argument in rendered):
        raise AssertionError("reference frontend argv is not line serializable")
    return ReferenceFrontendContract(
        image_tag=image_tag,
        image_id=image_id,
        source_adapter_sha256=source_adapter_sha256,
        builtin_header_sha256=builtin_header_sha256,
        compiler_argv_sha256=compiler_argv_sha256,
        compiler_argv=rendered,
    )


def _location(payload: bytes, offset: int) -> str:
    line = payload.count(b"\n", 0, offset) + 1
    previous = payload.rfind(b"\n", 0, offset)
    column = offset - previous
    return f"line {line}, column {column}"


def _error(payload: bytes, offset: int, message: str) -> ReferenceSourceError:
    return ReferenceSourceError(f"{message} at {_location(payload, offset)}")


def _comment_piece(payload: bytes, offset: int) -> tuple[_Piece, int]:
    if payload[offset + 1] == ord("/"):
        cursor = offset + 2
        while cursor < len(payload) and payload[cursor] not in (ord("\n"), ord("\r")):
            cursor += 1
        return _Piece("raw", payload[offset:cursor], offset), cursor
    closing = payload.find(b"*/", offset + 2)
    if closing < 0:
        raise _error(payload, offset, "unterminated block comment")
    end = closing + 2
    return _Piece("raw", payload[offset:end], offset), end


def _consume_suffix(payload: bytes, cursor: int) -> int:
    while cursor < len(payload) and payload[cursor] in b"fFlLuU":
        cursor += 1
    return cursor


def _number_piece(payload: bytes, offset: int) -> tuple[_Piece, int]:
    cursor = offset
    hexadecimal = (
        cursor + 1 < len(payload)
        and payload[cursor] == ord("0")
        and payload[cursor + 1] in (ord("x"), ord("X"))
    )
    floating = False

    if hexadecimal:
        cursor += 2
        mantissa_digits = 0
        while cursor < len(payload) and payload[cursor] in _HEX_DIGITS:
            cursor += 1
            mantissa_digits += 1
        has_point = cursor < len(payload) and payload[cursor] == ord(".")
        if has_point:
            floating = True
            cursor += 1
            while cursor < len(payload) and payload[cursor] in _HEX_DIGITS:
                cursor += 1
                mantissa_digits += 1
        if mantissa_digits == 0:
            raise _error(payload, offset, "hexadecimal constant has no digits")
        if cursor < len(payload) and payload[cursor] in (ord("p"), ord("P")):
            floating = True
            cursor += 1
            if cursor < len(payload) and payload[cursor] in (ord("+"), ord("-")):
                cursor += 1
            exponent_start = cursor
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
            if cursor == exponent_start:
                raise _error(payload, offset, "hexadecimal float has no exponent digits")
        elif has_point:
            raise _error(payload, offset, "hexadecimal float requires a binary exponent")
    else:
        if payload[cursor] == ord("."):
            floating = True
            cursor += 1
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
        else:
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
            if cursor < len(payload) and payload[cursor] == ord("."):
                floating = True
                cursor += 1
                while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                    cursor += 1
        if cursor < len(payload) and payload[cursor] in (ord("e"), ord("E")):
            floating = True
            cursor += 1
            if cursor < len(payload) and payload[cursor] in (ord("+"), ord("-")):
                cursor += 1
            exponent_start = cursor
            while cursor < len(payload) and payload[cursor] in _DECIMAL_DIGITS:
                cursor += 1
            if cursor == exponent_start:
                raise _error(payload, offset, "decimal float has no exponent digits")

    core_end = cursor
    end = _consume_suffix(payload, cursor)
    suffix = payload[core_end:end]
    if suffix:
        if any(value in b"fF" for value in suffix):
            floating = True
        if floating:
            if len(suffix) != 1 or suffix not in (b"f", b"F", b"l", b"L"):
                raise _error(payload, core_end, "invalid floating constant suffix")
        elif any(value not in b"uUlL" for value in suffix):
            raise _error(payload, core_end, "invalid integer constant suffix")

    core = payload[offset:core_end]
    if floating:
        if not hexadecimal and b"." not in core and b"e" not in core and b"E" not in core:
            core += b".0"
        return _Piece("float", core + b"f", offset), end

    if hexadecimal:
        magnitude = int(core[2:], 16)
    elif len(core) > 1 and core.startswith(b"0"):
        if any(value not in b"01234567" for value in core):
            raise _error(payload, offset, "invalid octal integer constant")
        magnitude = int(core, 8)
    else:
        magnitude = int(core, 10)
    if magnitude > 0xFFFFFFFF:
        raise _error(payload, offset, "integer constant exceeds the SysY 32-bit bit-pattern range")
    signed = magnitude if magnitude <= 0x7FFFFFFF else magnitude - 0x100000000
    if signed == -0x80000000:
        normalized = b"((int)(-2147483647 - 1))"
    else:
        normalized = f"((int){signed})".encode("ascii")
    return _Piece("integer", normalized, offset), end


def _lex(payload: bytes) -> list[_Piece]:
    pieces: list[_Piece] = []
    cursor = 0
    while cursor < len(payload):
        value = payload[cursor]
        if value == 0:
            raise _error(payload, cursor, "NUL byte is not valid SysY source")
        if value == ord("/") and cursor + 1 < len(payload) and payload[cursor + 1] in (
            ord("/"),
            ord("*"),
        ):
            piece, cursor = _comment_piece(payload, cursor)
            pieces.append(piece)
            continue
        if value in (ord('"'), ord("'")):
            raise _error(payload, cursor, "quoted literals are not part of the SysY lexical contract")
        if value in _IDENTIFIER_START:
            end = cursor + 1
            while end < len(payload) and payload[end] in _IDENTIFIER_CONTINUE:
                end += 1
            pieces.append(_Piece("identifier", payload[cursor:end], cursor))
            cursor = end
            continue
        if value in _DECIMAL_DIGITS or (
            value == ord(".")
            and cursor + 1 < len(payload)
            and payload[cursor + 1] in _DECIMAL_DIGITS
        ):
            piece, cursor = _number_piece(payload, cursor)
            pieces.append(piece)
            continue
        if value >= 0x80:
            raise _error(payload, cursor, "non-ASCII byte outside a comment")
        if value < 0x20 and value not in _WHITESPACE:
            raise _error(payload, cursor, "control byte is not valid SysY source")
        if value in _WHITESPACE:
            pieces.append(_Piece("raw", payload[cursor : cursor + 1], cursor))
            cursor += 1
            continue
        token = payload[cursor : cursor + 2]
        if token in _SYSY_TWO_BYTE_TOKENS:
            pieces.append(_Piece("raw", token, cursor))
            cursor += 2
            continue
        if token in _NON_SYSY_TWO_BYTE_TOKENS:
            raise _error(payload, cursor, "operator is not part of the SysY lexical contract")
        if value in _SYSY_ONE_BYTE_TOKENS:
            pieces.append(_Piece("raw", payload[cursor : cursor + 1], cursor))
            cursor += 1
            continue
        raise _error(payload, cursor, "byte is not part of the SysY lexical contract")
    return pieces


def _identifier_mapping(pieces: list[_Piece]) -> tuple[str | None, dict[bytes, bytes]]:
    identifiers = {
        piece.payload.decode("ascii") for piece in pieces if piece.kind == "identifier"
    }
    renamed = sorted(identifiers & _CXX_ONLY_KEYWORDS)
    if not renamed:
        return None, {}
    attempt = 0
    while True:
        prefix = f"__accela_sysy_cxx_{attempt}_"
        generated = {prefix + identifier for identifier in renamed}
        if generated.isdisjoint(identifiers):
            return prefix, {
                identifier.encode("ascii"): (prefix + identifier).encode("ascii")
                for identifier in renamed
            }
        attempt += 1


def adapt_source(payload: bytes) -> AdaptedSource:
    pieces = _lex(payload)
    prefix, mapping = _identifier_mapping(pieces)
    output: list[bytes] = []
    integers = 0
    floats = 0
    for piece in pieces:
        if piece.kind == "identifier":
            output.append(mapping.get(piece.payload, piece.payload))
        else:
            output.append(piece.payload)
        integers += piece.kind == "integer"
        floats += piece.kind == "float"
    adapted = b"".join(output)
    if adapted.count(b"\n") != payload.count(b"\n"):
        raise AssertionError("reference source adapter changed physical line count")
    return AdaptedSource(
        payload=adapted,
        identifier_prefix=prefix,
        renamed_identifiers=tuple(sorted(value.decode("ascii") for value in mapping)),
        integer_literal_count=integers,
        float_literal_count=floats,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and adapt the frozen SysY C++17 reference frontend."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    adapt = commands.add_parser("adapt", help="translate SysY tokens into C++17 input")
    adapt.add_argument("source", type=Path)
    adapt.add_argument("output", type=Path)
    contract = commands.add_parser(
        "contract", help="validate the frozen frontend and emit its exact argv"
    )
    contract.add_argument("--root", type=Path, required=True)
    contract.add_argument("--snapshot", type=Path, required=True)
    contract.add_argument("--frontend", choices=sorted(REFERENCE_FRONTEND_ARGV), required=True)
    contract.add_argument("--artifact-name", required=True)
    contract.add_argument("--argv-output", type=Path, required=True)
    return parser


def _adapt_command(args: argparse.Namespace) -> int:
    try:
        source = args.source.read_bytes()
    except OSError:
        print("reference-source: cannot read source", file=sys.stderr)
        return 2
    try:
        adapted = adapt_source(source)
    except ReferenceSourceError as exc:
        print(f"reference-source: {exc}", file=sys.stderr)
        return 2
    try:
        with args.output.open("xb") as stream:
            stream.write(adapted.payload)
    except FileExistsError:
        print("reference-source: output already exists", file=sys.stderr)
        return 2
    except OSError:
        print("reference-source: cannot write output", file=sys.stderr)
        return 2
    return 0


def _contract_command(args: argparse.Namespace) -> int:
    try:
        contract = load_reference_frontend_contract(
            root=args.root,
            snapshot_path=args.snapshot,
            frontend=args.frontend,
            artifact_name=args.artifact_name,
        )
    except ReferenceSourceError as exc:
        print(f"reference-source: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("reference-source: cannot read reference frontend contract", file=sys.stderr)
        return 2
    try:
        with args.argv_output.open("x", encoding="utf-8", newline="\n") as stream:
            for argument in contract.compiler_argv:
                stream.write(argument)
                stream.write("\n")
    except FileExistsError:
        print("reference-source: argv output already exists", file=sys.stderr)
        return 2
    except OSError:
        print("reference-source: cannot write argv output", file=sys.stderr)
        return 2
    print(contract.image_tag)
    print(contract.image_id)
    print(contract.source_adapter_sha256)
    print(contract.builtin_header_sha256)
    print(contract.compiler_argv_sha256)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "adapt":
        return _adapt_command(args)
    if args.command == "contract":
        return _contract_command(args)
    raise AssertionError(f"unhandled reference-source command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
