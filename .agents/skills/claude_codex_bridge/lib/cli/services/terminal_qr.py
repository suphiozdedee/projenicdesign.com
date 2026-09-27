from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import struct
import zlib


_BYTE_MODE = 0b0100

# QR Code Model 2, error correction level L. The onboarding payload is compact
# JSON, so byte mode and these early versions are enough while keeping the
# implementation dependency-free for npm/source installs.
_VERSION_BLOCKS_L: dict[int, tuple[int, tuple[int, ...]]] = {
    1: (7, (19,)),
    2: (10, (34,)),
    3: (15, (55,)),
    4: (20, (80,)),
    5: (26, (108,)),
    6: (18, (68, 68)),
    7: (20, (78, 78)),
    8: (24, (97, 97)),
    9: (30, (116, 116)),
    10: (18, (68, 68, 69, 69)),
    11: (20, (81, 81, 81, 81)),
    12: (24, (92, 92, 93, 93)),
    13: (26, (107, 107, 107, 107)),
    14: (30, (115, 115, 115, 116)),
    15: (22, (87, 87, 87, 87, 87, 88)),
    16: (24, (98, 98, 98, 98, 98, 99)),
    17: (28, (107, 108, 108, 108, 108, 108)),
    18: (30, (120, 120, 120, 120, 120, 121)),
    19: (28, (113, 113, 113, 114, 114, 114, 114)),
    20: (28, (107, 107, 107, 108, 108, 108, 108, 108)),
    21: (28, (116, 116, 116, 116, 117, 117, 117, 117)),
    22: (28, (111, 111, 112, 112, 112, 112, 112, 112, 112)),
    23: (30, (121, 121, 121, 121, 122, 122, 122, 122, 122)),
    24: (30, (117, 117, 117, 117, 117, 117, 118, 118, 118, 118)),
    25: (26, (106, 106, 106, 106, 106, 106, 106, 106, 107, 107, 107, 107)),
    26: (28, (114, 114, 114, 114, 114, 114, 114, 114, 114, 114, 115, 115)),
    27: (30, (122, 122, 122, 122, 122, 122, 122, 122, 123, 123, 123, 123)),
}

_ALIGNMENT_POSITIONS: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
    11: (6, 30, 54),
    12: (6, 32, 58),
    13: (6, 34, 62),
    14: (6, 26, 46, 66),
    15: (6, 26, 48, 70),
    16: (6, 26, 50, 74),
    17: (6, 30, 54, 78),
    18: (6, 30, 56, 82),
    19: (6, 30, 58, 86),
    20: (6, 34, 62, 90),
    21: (6, 28, 50, 72, 94),
    22: (6, 26, 50, 74, 98),
    23: (6, 30, 54, 78, 102),
    24: (6, 28, 54, 80, 106),
    25: (6, 32, 58, 84, 110),
    26: (6, 30, 58, 86, 114),
    27: (6, 34, 62, 90, 118),
}


@dataclass(frozen=True)
class TerminalQrCode:
    version: int
    modules: tuple[tuple[bool, ...], ...]

    @property
    def size(self) -> int:
        return len(self.modules)


def make_terminal_qr(text: str) -> TerminalQrCode:
    data = str(text).encode("utf-8")
    version, data_lengths, ecc_len = _select_version(data)
    data_codewords = _encode_data_codewords(data, version, sum(data_lengths))
    blocks = _split_blocks(data_codewords, data_lengths)
    ecc_blocks = [_reed_solomon_remainder(block, ecc_len) for block in blocks]
    codewords = _interleave(blocks, ecc_blocks)
    modules, function_modules = _blank_matrix(version)
    _draw_function_patterns(modules, function_modules, version)
    _draw_codewords(modules, function_modules, _codeword_bits(codewords))
    mask = _best_mask(modules, function_modules)
    _apply_mask(modules, function_modules, mask)
    _draw_format_bits(modules, function_modules, mask)
    if version >= 7:
        _draw_version_bits(modules, function_modules, version)
    return TerminalQrCode(
        version=version,
        modules=tuple(tuple(bool(value) for value in row) for row in modules),
    )


def render_terminal_qr(
    text: str, *, ansi: bool = False, quiet_zone: int = 4, compact: bool = False
) -> tuple[str, ...]:
    qr = make_terminal_qr(text)
    border = max(0, int(quiet_zone))
    if compact:
        return _render_compact_terminal_qr(qr, ansi=ansi, border=border)
    light = "\x1b[47m  \x1b[0m" if ansi else "██"
    dark = "\x1b[40m  \x1b[0m" if ansi else "  "
    rows: list[str] = []
    width = qr.size + border * 2
    light_row = light * width
    rows.extend([light_row] * border)
    for row in qr.modules:
        rendered = [light] * border
        rendered.extend(dark if value else light for value in row)
        rendered.extend([light] * border)
        rows.append("".join(rendered))
    rows.extend([light_row] * border)
    return tuple(rows)


def render_terminal_qr_png(
    text: str,
    *,
    quiet_zone: int = 4,
    module_size: int = 8,
) -> bytes:
    qr = make_terminal_qr(text)
    border = max(0, int(quiet_zone))
    scale = max(1, int(module_size))
    module_width = qr.size + border * 2
    pixel_width = module_width * scale
    raw = bytearray()

    for module_row in range(module_width):
        source_row = (
            qr.modules[module_row - border]
            if border <= module_row < border + qr.size
            else ()
        )
        scanline = bytearray((0,))
        for module_col in range(module_width):
            dark = (
                bool(source_row[module_col - border])
                if border <= module_col < border + qr.size and source_row
                else False
            )
            scanline.extend(bytes((0 if dark else 255,)) * scale)
        for _ in range(scale):
            raw.extend(scanline)

    header = struct.pack(
        ">IIBBBBB",
        pixel_width,
        pixel_width,
        8,
        0,
        0,
        0,
        0,
    )
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9)),
            _png_chunk(b"IEND", b""),
        )
    )


def write_terminal_qr_png(
    path: str | Path,
    text: str,
    *,
    quiet_zone: int = 4,
    module_size: int = 8,
) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = render_terminal_qr_png(
        text,
        quiet_zone=quiet_zone,
        module_size=module_size,
    )
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, 0o600)
    opened = True
    try:
        with os.fdopen(fd, "wb") as handle:
            opened = False
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except BaseException:
        if opened:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def _render_compact_terminal_qr(
    qr: TerminalQrCode, *, ansi: bool, border: int
) -> tuple[str, ...]:
    width = qr.size + border * 2
    light_row = [False] * width
    rows: list[list[bool]] = [light_row[:] for _ in range(border)]
    rows.extend(([False] * border) + list(row) + ([False] * border) for row in qr.modules)
    rows.extend([light_row[:] for _ in range(border)])

    rendered: list[str] = []
    for index in range(0, len(rows), 2):
        top = rows[index]
        bottom = rows[index + 1] if index + 1 < len(rows) else light_row
        rendered.append(
            "".join(
                _render_compact_cell(top_dark, bottom_dark, ansi=ansi)
                for top_dark, bottom_dark in zip(top, bottom)
            )
        )
    return tuple(rendered)


def _render_compact_cell(top_dark: bool, bottom_dark: bool, *, ansi: bool) -> str:
    if ansi:
        if top_dark and bottom_dark:
            return "\x1b[40m \x1b[0m"
        if not top_dark and not bottom_dark:
            return "\x1b[47m \x1b[0m"
        if top_dark and not bottom_dark:
            return "\x1b[30;47m▀\x1b[0m"
        return "\x1b[37;40m▀\x1b[0m"
    if top_dark and bottom_dark:
        return " "
    if not top_dark and not bottom_dark:
        return "█"
    if top_dark and not bottom_dark:
        return "▄"
    return "▀"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", checksum)
    )


def _select_version(data: bytes) -> tuple[int, tuple[int, ...], int]:
    for version, (ecc_len, data_lengths) in _VERSION_BLOCKS_L.items():
        char_count_bits = 8 if version <= 9 else 16
        required_bits = 4 + char_count_bits + len(data) * 8
        capacity_bits = sum(data_lengths) * 8
        if required_bits <= capacity_bits:
            return version, data_lengths, ecc_len
    raise ValueError("pairing QR payload is too large for terminal QR generation")


def _encode_data_codewords(
    data: bytes, version: int, capacity_codewords: int
) -> list[int]:
    bits: list[int] = []
    _append_bits(bits, _BYTE_MODE, 4)
    _append_bits(bits, len(data), 8 if version <= 9 else 16)
    for value in data:
        _append_bits(bits, value, 8)
    capacity_bits = capacity_codewords * 8
    _append_bits(bits, 0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = [
        int("".join(str(bit) for bit in bits[index : index + 8]), 2)
        for index in range(0, len(bits), 8)
    ]
    pad = 0xEC
    while len(codewords) < capacity_codewords:
        codewords.append(pad)
        pad = 0x11 if pad == 0xEC else 0xEC
    return codewords


def _append_bits(bits: list[int], value: int, count: int) -> None:
    for shift in range(count - 1, -1, -1):
        bits.append((value >> shift) & 1)


def _split_blocks(
    data_codewords: list[int], lengths: tuple[int, ...]
) -> list[list[int]]:
    blocks: list[list[int]] = []
    offset = 0
    for length in lengths:
        blocks.append(data_codewords[offset : offset + length])
        offset += length
    return blocks


def _interleave(blocks: list[list[int]], ecc_blocks: list[list[int]]) -> list[int]:
    result: list[int] = []
    for index in range(max(len(block) for block in blocks)):
        for block in blocks:
            if index < len(block):
                result.append(block[index])
    for index in range(max(len(block) for block in ecc_blocks)):
        for block in ecc_blocks:
            if index < len(block):
                result.append(block[index])
    return result


def _codeword_bits(codewords: list[int]) -> list[int]:
    bits: list[int] = []
    for value in codewords:
        _append_bits(bits, value, 8)
    return bits


def _gf_multiply(left: int, right: int) -> int:
    product = 0
    while right:
        if right & 1:
            product ^= left
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
        right >>= 1
    return product


def _gf_pow(power: int) -> int:
    value = 1
    for _ in range(power):
        value = _gf_multiply(value, 2)
    return value


def _reed_solomon_generator(degree: int) -> list[int]:
    coefficients = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for index in range(degree):
            coefficients[index] = _gf_multiply(coefficients[index], root)
            if index + 1 < degree:
                coefficients[index] ^= coefficients[index + 1]
        root = _gf_multiply(root, 2)
    return coefficients


def _reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    generator = _reed_solomon_generator(degree)
    result = [0] * degree
    for value in data:
        factor = value ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(generator):
            result[index] ^= _gf_multiply(coefficient, factor)
    return result


def _blank_matrix(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    size = version * 4 + 17
    return (
        [[False] * size for _ in range(size)],
        [[False] * size for _ in range(size)],
    )


def _draw_function_patterns(
    modules: list[list[bool]], function_modules: list[list[bool]], version: int
) -> None:
    size = len(modules)
    _draw_finder(modules, function_modules, 3, 3)
    _draw_finder(modules, function_modules, size - 4, 3)
    _draw_finder(modules, function_modules, 3, size - 4)
    for row in _ALIGNMENT_POSITIONS[version]:
        for col in _ALIGNMENT_POSITIONS[version]:
            if function_modules[row][col]:
                continue
            _draw_alignment(modules, function_modules, row, col)
    for index in range(8, size - 8):
        if not function_modules[6][index]:
            _set_function(modules, function_modules, 6, index, index % 2 == 0)
        if not function_modules[index][6]:
            _set_function(modules, function_modules, index, 6, index % 2 == 0)
    _set_function(modules, function_modules, size - 8, 8, True)
    _draw_format_bits(modules, function_modules, 0)
    if version >= 7:
        _draw_version_bits(modules, function_modules, version)


def _draw_finder(
    modules: list[list[bool]],
    function_modules: list[list[bool]],
    center_row: int,
    center_col: int,
) -> None:
    size = len(modules)
    for row in range(center_row - 4, center_row + 5):
        for col in range(center_col - 4, center_col + 5):
            if 0 <= row < size and 0 <= col < size:
                distance = max(abs(row - center_row), abs(col - center_col))
                _set_function(
                    modules, function_modules, row, col, distance not in {2, 4}
                )


def _draw_alignment(
    modules: list[list[bool]],
    function_modules: list[list[bool]],
    center_row: int,
    center_col: int,
) -> None:
    for row in range(center_row - 2, center_row + 3):
        for col in range(center_col - 2, center_col + 3):
            distance = max(abs(row - center_row), abs(col - center_col))
            _set_function(modules, function_modules, row, col, distance != 1)


def _set_function(
    modules: list[list[bool]],
    function_modules: list[list[bool]],
    row: int,
    col: int,
    value: bool,
) -> None:
    modules[row][col] = value
    function_modules[row][col] = True


def _draw_codewords(
    modules: list[list[bool]], function_modules: list[list[bool]], bits: list[int]
) -> None:
    size = len(modules)
    bit_index = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for current_col in (col, col - 1):
                if function_modules[row][current_col]:
                    continue
                modules[row][current_col] = bit_index < len(bits) and bool(
                    bits[bit_index]
                )
                bit_index += 1
        upward = not upward
        col -= 2


def _mask_bit(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _best_mask(modules: list[list[bool]], function_modules: list[list[bool]]) -> int:
    best_mask = 0
    best_penalty: int | None = None
    for mask in range(8):
        candidate = [row[:] for row in modules]
        _apply_mask(candidate, function_modules, mask)
        penalty = _penalty(candidate)
        if best_penalty is None or penalty < best_penalty:
            best_penalty = penalty
            best_mask = mask
    return best_mask


def _apply_mask(
    modules: list[list[bool]], function_modules: list[list[bool]], mask: int
) -> None:
    for row in range(len(modules)):
        for col in range(len(modules)):
            if not function_modules[row][col] and _mask_bit(mask, row, col):
                modules[row][col] = not modules[row][col]


def _penalty(modules: list[list[bool]]) -> int:
    size = len(modules)
    penalty = 0
    rows = list(modules)
    cols = [[modules[row][col] for row in range(size)] for col in range(size)]
    for line in rows + cols:
        run_color = line[0]
        run_len = 1
        for value in line[1:]:
            if value == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    penalty += run_len - 2
                run_color = value
                run_len = 1
        if run_len >= 5:
            penalty += run_len - 2
    for row in range(size - 1):
        for col in range(size - 1):
            color = modules[row][col]
            if all(
                modules[row + dr][col + dc] == color
                for dr in range(2)
                for dc in range(2)
            ):
                penalty += 3
    pattern = (True, False, True, True, True, False, True, False, False, False, False)
    reverse = tuple(reversed(pattern))
    for line in rows + cols:
        for index in range(len(line) - 10):
            window = tuple(line[index : index + 11])
            if window == pattern or window == reverse:
                penalty += 40
    dark = sum(1 for row in modules for value in row if value)
    percent = dark * 100 // (size * size)
    penalty += abs(percent - 50) // 5 * 10
    return penalty


def _draw_format_bits(
    modules: list[list[bool]], function_modules: list[list[bool]], mask: int
) -> None:
    size = len(modules)
    bits = _format_bits(mask)
    positions = [
        (8, 0),
        (8, 1),
        (8, 2),
        (8, 3),
        (8, 4),
        (8, 5),
        (8, 7),
        (8, 8),
        (7, 8),
        (5, 8),
        (4, 8),
        (3, 8),
        (2, 8),
        (1, 8),
        (0, 8),
    ]
    for index, (row, col) in enumerate(positions):
        _set_function(
            modules, function_modules, row, col, ((bits >> (14 - index)) & 1) != 0
        )
    positions = [
        (size - 1, 8),
        (size - 2, 8),
        (size - 3, 8),
        (size - 4, 8),
        (size - 5, 8),
        (size - 6, 8),
        (size - 7, 8),
        (8, size - 8),
        (8, size - 7),
        (8, size - 6),
        (8, size - 5),
        (8, size - 4),
        (8, size - 3),
        (8, size - 2),
        (8, size - 1),
    ]
    for index, (row, col) in enumerate(positions):
        _set_function(
            modules, function_modules, row, col, ((bits >> (14 - index)) & 1) != 0
        )
    _set_function(modules, function_modules, size - 8, 8, True)


def _format_bits(mask: int) -> int:
    data = (1 << 3) | mask  # Error correction level L has format bits 01.
    value = data << 10
    generator = 0x537
    for shift in range(14, 9, -1):
        if (value >> shift) & 1:
            value ^= generator << (shift - 10)
    return ((data << 10) | value) ^ 0x5412


def _draw_version_bits(
    modules: list[list[bool]], function_modules: list[list[bool]], version: int
) -> None:
    size = len(modules)
    bits = _version_bits(version)
    for index in range(18):
        bit = ((bits >> index) & 1) != 0
        row = index // 3
        col = index % 3 + size - 11
        _set_function(modules, function_modules, row, col, bit)
        _set_function(modules, function_modules, col, row, bit)


def _version_bits(version: int) -> int:
    value = version << 12
    generator = 0x1F25
    for shift in range(17, 11, -1):
        if (value >> shift) & 1:
            value ^= generator << (shift - 12)
    return (version << 12) | value


__all__ = [
    "TerminalQrCode",
    "make_terminal_qr",
    "render_terminal_qr",
    "render_terminal_qr_png",
    "write_terminal_qr_png",
]
