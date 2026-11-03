"""A small QR code encoder (byte mode, error correction level M, versions 1-6), enough for Steam's
sign-in links, so the page needs no outside library. Written from the QR specification (ISO/IEC 18004)."""

# version: (data codewords per block, number of blocks, EC codewords per block) for level M
BLOCKS = {1: (16, 1, 10), 2: (28, 1, 16), 3: (44, 1, 26), 4: (32, 2, 18), 5: (43, 2, 24), 6: (27, 4, 16)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
LEVEL_M = 0b00

# GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1
EXP, LOG = [0] * 512, [0] * 256
_v = 1
for _i in range(255):
    EXP[_i], LOG[_v] = _v, _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _mul(a, b):
    return 0 if not a or not b else EXP[LOG[a] + LOG[b]]


def _ec_codewords(data, n):
    """Reed-Solomon remainder of data * x^n by the generator (x - a^0)...(x - a^(n-1))."""
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, EXP[i])
        gen = nxt
    rem = list(data) + [0] * n
    for i in range(len(data)):
        factor = rem[i]
        if factor:
            for j in range(1, len(gen)):
                rem[i + j] ^= _mul(gen[j], factor)
    return rem[len(data):]


def _codewords(text):
    data = text.encode("utf-8")
    for version, (per_block, blocks, _) in BLOCKS.items():
        if len(data) + 2 <= per_block * blocks:  # 4-bit mode + 8-bit count + data + terminator fit
            break
    else:
        raise ValueError("too long for a small QR code")
    per_block, blocks, ec_len = BLOCKS[version]
    capacity = per_block * blocks
    bits = "0100" + format(len(data), "08b") + "".join(format(b, "08b") for b in data)
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    filler = 0
    while len(words) < capacity:  # alternate the two pad bytes the spec names
        words.append((0xEC, 0x11)[filler % 2])
        filler += 1
    groups = [words[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    ecs = [_ec_codewords(g, ec_len) for g in groups]
    out = [g[i] for i in range(per_block) for g in groups]
    out += [e[i] for i in range(ec_len) for e in ecs]
    return version, out


class _Grid:
    def __init__(self, version):
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark, fixed=True):
        self.dark[y][x] = dark
        self.fixed[y][x] = self.fixed[y][x] or fixed


def _function_patterns(g, version):
    n = g.size
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):  # finders with their white separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    ring = max(abs(dx), abs(dy))
                    g.set(x, y, ring not in (2, 4))
    for i in range(8, n - 8):  # timing
        g.set(i, 6, i % 2 == 0)
        g.set(6, i, i % 2 == 0)
    centers = ALIGN[version]
    for cy in centers:
        for cx in centers:
            if g.fixed[cy][cx]:
                continue  # overlaps a finder
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    g.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    _format_bits(g, 0)  # reserve the format areas (real bits go in after masking)
    g.set(8, n - 8, True)


def _format_bits(g, mask):
    data = (LEVEL_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412
    bit = lambda i: (bits >> i) & 1 == 1
    n = g.size
    for i in range(6):
        g.set(8, i, bit(i))
    g.set(8, 7, bit(6))
    g.set(8, 8, bit(7))
    g.set(7, 8, bit(8))
    for i in range(9, 15):
        g.set(14 - i, 8, bit(i))
    for i in range(8):
        g.set(n - 1 - i, 8, bit(i))
    for i in range(8, 15):
        g.set(8, n - 15 + i, bit(i))
    g.set(8, n - 8, True)


def _place(g, words):
    bits = [(w >> (7 - i)) & 1 == 1 for w in words for i in range(8)]
    n, k, right = g.size, 0, g.size - 1
    while right >= 1:
        if right == 6:
            right = 5  # the vertical timing column is skipped
        upward = ((right + 1) & 2) == 0
        for vert in range(n):
            y = n - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not g.fixed[y][x]:
                    g.dark[y][x] = bits[k] if k < len(bits) else False
                    k += 1
        right -= 2


MASKS = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
         lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
         lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
         lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]


def _apply_mask(g, m):
    for y in range(g.size):
        for x in range(g.size):
            if not g.fixed[y][x] and MASKS[m](x, y):
                g.dark[y][x] = not g.dark[y][x]


def _penalty(rows):
    n, score = len(rows), 0
    lines = rows + [list(col) for col in zip(*rows)]
    finder_a, finder_b = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in lines:
        run = 1
        for i in range(1, n + 1):
            if i < n and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        vals = [int(v) for v in line]
        for i in range(n - 10):
            if vals[i:i + 11] in (finder_a, finder_b):
                score += 40
    for y in range(n - 1):
        for x in range(n - 1):
            if rows[y][x] == rows[y][x + 1] == rows[y + 1][x] == rows[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, rows))
    score += abs(dark * 20 - n * n * 10) // (n * n) * 10
    return score


def matrix(text):
    """Rows of booleans (True = dark), without the quiet zone."""
    version, words = _codewords(text)
    best = None
    for m in range(8):
        g = _Grid(version)
        _function_patterns(g, version)
        _place(g, words)
        _apply_mask(g, m)
        _format_bits(g, m)
        score = _penalty(g.dark)
        if best is None or score < best[0]:
            best = (score, g.dark)
    return best[1]


def svg(text, scale=8, quiet=4):
    rows = matrix(text)
    size = (len(rows) + 2 * quiet) * scale
    path = "".join(f"M{(x + quiet) * scale},{(y + quiet) * scale}h{scale}v{scale}h-{scale}z"
                   for y, row in enumerate(rows) for x, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
            f'<rect width="100%" height="100%" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
