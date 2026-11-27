"""A small QR code writer, enough for the address an authenticator app scans.

Byte mode, error correction level M, versions 1 to 10 (up to 213 bytes), drawn
as an SVG. Written here rather than pulled in, so the console and its packages
gain no dependency for one picture.
"""

from __future__ import annotations

from markupsafe import Markup

# Per version, at level M: (error correction codewords per block,
#                           [(number of blocks, data codewords in each), ...])
BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
ALIGNMENT = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}
LEVEL_M = 0b00
MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

# The field the error correction works in: GF(256), polynomial x^8+x^4+x^3+x^2+1.
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _correction(data: list[int], count: int) -> list[int]:
    """Reed-Solomon: the `count` codewords that let a reader repair `data`."""
    generator = [1]
    for i in range(count):
        following = [0] * (len(generator) + 1)
        for j, coefficient in enumerate(generator):
            following[j] ^= coefficient
            following[j + 1] ^= EXP[LOG[coefficient] + i] if coefficient else 0
        generator = following
    remainder = [0] * count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for j in range(count):
                remainder[j] ^= EXP[LOG[generator[j + 1]] + LOG[factor]]
    return remainder


def _capacity(version: int) -> int:
    return sum(blocks * size for blocks, size in BLOCKS[version][1])


def _codewords(data: bytes) -> tuple[int, list[int]]:
    """The smallest version that holds `data`, and every codeword in drawing order."""
    for version in BLOCKS:
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= 8 * _capacity(version):
            break
    else:
        raise ValueError("That is too long for a QR code here.")
    bits = f"0100{len(data):0{count_bits}b}" + "".join(f"{byte:08b}" for byte in data)
    room = 8 * _capacity(version)
    bits += "0" * min(4, room - len(bits))
    bits += "0" * (-len(bits) % 8)
    stream = [int(bits[i : i + 8], 2) for i in range(0, len(bits), 8)]
    padding = (0xEC, 0x11)
    stream += [padding[i % 2] for i in range(_capacity(version) - len(stream))]

    correction, groups = BLOCKS[version]
    blocks, at = [], 0
    for number, size in groups:
        for _ in range(number):
            blocks.append(stream[at : at + size])
            at += size
    checks = [_correction(block, correction) for block in blocks]
    # Readers expect the blocks interleaved: first codeword of each, then second...
    ordered = [
        block[i] for i in range(max(len(b) for b in blocks)) for block in blocks if i < len(block)
    ]
    ordered += [check[i] for i in range(correction) for check in checks]
    return version, ordered


def _bch(value: int, generator: int, width: int) -> int:
    """`value` followed by its check bits, for the format and version markings."""
    shifted = value << width
    top = generator.bit_length()
    remainder = shifted
    while remainder.bit_length() >= top:
        remainder ^= generator << (remainder.bit_length() - top)
    return shifted | remainder


def _skeleton(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    """The fixed patterns of a symbol, and which modules they (and the markings) own."""
    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    taken = [[False] * size for _ in range(size)]

    def put(row: int, column: int, value: bool) -> None:
        if 0 <= row < size and 0 <= column < size:
            dark[row][column] = value
            taken[row][column] = True

    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):  # finders and their margins
        for r in range(-1, 8):
            for c in range(-1, 8):
                ring = max(abs(r - 3), abs(c - 3))
                put(top + r, left + c, ring != 2 and ring <= 3)
    for i in range(8, size - 8):  # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    centres = ALIGNMENT[version]
    for row in centres:
        for column in centres:
            if (row, column) in ((6, 6), (6, centres[-1]), (centres[-1], 6)):
                continue  # that corner has a finder
            for r in range(-2, 3):
                for c in range(-2, 3):
                    put(row + r, column + c, max(abs(r), abs(c)) != 1)
    for i in range(9):  # room for the format bits
        for row, column in ((8, i), (i, 8)):
            taken[row][column] = True
    for i in range(8):
        taken[8][size - 1 - i] = True
        taken[size - 1 - i][8] = True
    put(size - 8, 8, True)
    if version >= 7:
        marking = _bch(version, 0x1F25, 12)
        for i in range(18):
            bit = bool(marking >> i & 1)
            put(i // 3, size - 11 + i % 3, bit)
            put(size - 11 + i % 3, i // 3, bit)
    return dark, taken


def _format(dark: list[list[bool]], mask: int) -> None:
    size = len(dark)
    bits = _bch(LEVEL_M << 3 | mask, 0x537, 10) ^ 0x5412
    for i in range(15):
        bit = bool(bits >> i & 1)
        # One copy around the top-left finder...
        if i < 6:
            dark[i][8] = bit
        elif i < 8:
            dark[i + 1][8] = bit
        elif i == 8:
            dark[8][7] = bit
        else:
            dark[8][14 - i] = bit
        # ... and one split between the other two.
        if i < 8:
            dark[8][size - 1 - i] = bit
        else:
            dark[size - 15 + i][8] = bit


def _penalty(dark: list[list[bool]]) -> int:
    """How hard this symbol would be to read; the standard's four measures."""
    size = len(dark)
    score = 0
    lines = dark + [list(column) for column in zip(*dark, strict=True)]
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
                continue
            if run >= 5:
                score += run - 2
            run = 1
        text = "".join("1" if module else "0" for module in line)
        for pattern in ("10111010000", "00001011101"):
            start = text.find(pattern)
            while start != -1:
                score += 40
                start = text.find(pattern, start + 1)
    for row in range(size - 1):
        for column in range(size - 1):
            block = dark[row][column]
            if (
                block == dark[row][column + 1]
                and block == dark[row + 1][column]
                and block == dark[row + 1][column + 1]
            ):
                score += 3
    share = sum(map(sum, dark)) * 100 // (size * size)
    score += 10 * (abs(share - 50) // 5)
    return score


def matrix(text: str) -> list[list[bool]]:
    """The modules of a QR code for `text`: rows of True (dark) and False."""
    version, codewords = _codewords(text.encode())
    plain, taken = _skeleton(version)
    size = len(plain)
    bits = [byte >> shift & 1 == 1 for byte in codewords for shift in range(7, -1, -1)]
    # Data snakes up and down in two-module columns from the bottom right.
    at = 0
    column = size - 1
    upwards = True
    while column > 0:
        if column == 6:
            column -= 1  # the timing column is skipped
        for row in range(size - 1, -1, -1) if upwards else range(size):
            for c in (column, column - 1):
                if not taken[row][c]:
                    plain[row][c] = bits[at] if at < len(bits) else False
                    at += 1
        upwards = not upwards
        column -= 2

    best = None
    for number, flips in enumerate(MASKS):
        masked = [
            [plain[r][c] != (not taken[r][c] and flips(r, c)) for c in range(size)]
            for r in range(size)
        ]
        _format(masked, number)
        score = _penalty(masked)
        if best is None or score < best[0]:
            best = (score, masked)
    return best[1]


def svg(text: str, *, quiet: int = 4) -> Markup:
    """An inline SVG of the QR code for `text` (black on white, whatever the theme)."""
    modules = matrix(text)
    side = len(modules) + 2 * quiet
    path = "".join(
        f"M{column + quiet},{row + quiet}h1v1h-1z"
        for row, line in enumerate(modules)
        for column, module in enumerate(line)
        if module
    )
    # Only numbers worked out here go into the markup.
    return Markup(  # noqa: S704
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{side}" height="{side}" fill="#fff"/>'
        f'<path d="{path}" fill="#000"/></svg>'
    )
