"""QR codes for the sign-in screen: byte mode, error correction level M, versions 1 to 6 (up to
106 bytes), which is plenty for Steam's sign-in links. Written for Card Farm from the QR Code
standard (ISO/IEC 18004); the two tables are figures from the standard."""
import itertools
import re

# version -> (blocks, data codewords per block, error correction codewords per block), level M
LEVEL_M = {1: (1, 16, 10), 2: (1, 28, 16), 3: (1, 44, 26), 4: (2, 32, 18), 5: (2, 43, 24), 6: (4, 27, 16)}
# version -> rows (and columns) holding alignment pattern centres
ALIGNMENT = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34)}


# --- error correction: Reed-Solomon over GF(256), field polynomial x^8 + x^4 + x^3 + x^2 + 1 --------
def _field():
    powers, logs, value = [], {}, 1
    for exponent in range(255):
        powers.append(value)
        logs[value] = exponent
        value <<= 1
        if value > 0xFF:
            value ^= 0b100011101
    return powers, logs


POWERS, LOGS = _field()


def _times(a, b):
    return POWERS[(LOGS[a] + LOGS[b]) % 255] if a and b else 0


def _check_words(block, count):
    """The `count` error correction codewords for one block: the remainder of block * x^count
    divided by (x - 1)(x - a)(x - a^2)...(x - a^(count-1)), highest power first."""
    divisor = [1]
    for k in range(count):
        root, longer = POWERS[k], divisor + [0]
        for i, coef in enumerate(divisor):
            longer[i + 1] ^= _times(coef, root)
        divisor = longer
    remainder = list(block) + [0] * count
    for i in range(len(block)):
        lead = remainder[i]
        for j in range(1, count + 1):
            remainder[i + j] ^= _times(divisor[j], lead)
    return remainder[len(block):]


# --- the message ---------------------------------------------------------------------------------
def _message(data):
    """(version, every codeword in the order it's drawn) for these bytes."""
    for version, (blocks, per_block, _) in LEVEL_M.items():
        capacity = blocks * per_block
        if 4 + 8 + 8 * len(data) <= 8 * capacity:
            break
    else:
        raise ValueError("too long for a small QR code")
    blocks, per_block, check_count = LEVEL_M[version]
    bits = "0100" + f"{len(data):08b}" + "".join(f"{byte:08b}" for byte in data)  # byte mode, count, data
    bits += "0" * min(4, 8 * capacity - len(bits))  # terminator
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    words += itertools.islice(itertools.cycle((0b11101100, 0b00010001)), capacity - len(words))
    groups = [words[n * per_block:(n + 1) * per_block] for n in range(blocks)]
    checks = [_check_words(group, check_count) for group in groups]
    interleaved = [group[i] for i in range(per_block) for group in groups]
    interleaved += [check[i] for i in range(check_count) for check in checks]
    return version, interleaved


# --- the symbol ----------------------------------------------------------------------------------
def _format_cells(size):
    """Both copies of the 15 format bits as (row, column), most significant bit first."""
    near_top_left = [(8, c) for c in (0, 1, 2, 3, 4, 5, 7, 8)] + [(r, 8) for r in (7, 5, 4, 3, 2, 1, 0)]
    split = [(r, 8) for r in range(size - 1, size - 8, -1)] + [(8, c) for c in range(size - 8, size)]
    return near_top_left, split


def _format_word(mask):
    """Level M (00) and the mask number, a BCH(15,5) check, then the standard's XOR pattern."""
    head = mask  # 0b00 << 3 | mask
    remainder = head << 10
    for top in range(14, 9, -1):
        if remainder >> top & 1:
            remainder ^= 0b10100110111 << (top - 10)
    return (head << 10 | remainder) ^ 0b101010000010010


def _fixed_parts(version):
    """(dark, reserved) grids holding the finder, timing and alignment patterns, the dark module
    and the (still empty) format areas. Indexed [row][column]."""
    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def put(r, c, is_dark):
        dark[r][c], reserved[r][c] = is_dark, True

    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):  # finders, with their light border
        for r in range(max(top - 1, 0), min(top + 8, size)):
            for c in range(max(left - 1, 0), min(left + 8, size)):
                dr, dc = r - top, c - left
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                put(r, c, inside and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)))
    for i in range(8, size - 8):  # timing lines
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for r in ALIGNMENT[version]:
        for c in ALIGNMENT[version]:
            if reserved[r][c]:
                continue  # that spot belongs to a finder
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, abs(dr) == 2 or abs(dc) == 2 or dr == dc == 0)
    for cells in _format_cells(size):
        for r, c in cells:
            put(r, c, False)
    put(size - 8, 8, True)  # always dark
    return dark, reserved


def _data_cells(reserved):
    """Free cells in drawing order: two-wide columns from the right, alternately up and down,
    stepping over the vertical timing line."""
    size = len(reserved)
    right, upward = size - 1, True
    while right > 0:
        if right == 6:
            right = 5
        for r in (range(size - 1, -1, -1) if upward else range(size)):
            for c in (right, right - 1):
                if not reserved[r][c]:
                    yield r, c
        upward = not upward
        right -= 2


def _masked(mask, r, c):
    """The standard's eight mask conditions (its i is the row, j the column)."""
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


FINDER_LIKE = re.compile("(?=00001011101|10111010000)")


def _penalty(dark):
    """The standard's four penalty rules; the mask with the lowest total is used."""
    size, score = len(dark), 0
    for line in dark + [list(column) for column in zip(*dark)]:
        for _, run in itertools.groupby(line):
            length = len(list(run))
            if length >= 5:
                score += 3 + length - 5
        score += 40 * len(FINDER_LIKE.findall("".join("1" if d else "0" for d in line)))
    for r in range(size - 1):
        for c in range(size - 1):
            if dark[r][c] == dark[r][c + 1] == dark[r + 1][c] == dark[r + 1][c + 1]:
                score += 3
    share = 100 * sum(map(sum, dark)) / size ** 2
    return score + 10 * int(abs(share - 50) // 5)


def matrix(text):
    """Rows of booleans (True = dark), without the quiet zone."""
    version, words = _message(text.encode("utf-8"))
    bits = [word >> (7 - k) & 1 for word in words for k in range(8)]
    base, reserved = _fixed_parts(version)
    cells = list(_data_cells(reserved))
    best = None
    for mask in range(8):
        dark = [row[:] for row in base]
        for n, (r, c) in enumerate(cells):  # leftover cells get 0 bits
            dark[r][c] = bool(n < len(bits) and bits[n]) != _masked(mask, r, c)
        word = _format_word(mask)
        for copy in _format_cells(len(dark)):
            for k, (r, c) in enumerate(copy):
                dark[r][c] = bool(word >> (14 - k) & 1)
        score = _penalty(dark)
        if best is None or score < best[0]:
            best = (score, dark)
    return best[1]


def svg(text, quiet=4):
    """The code as an SVG drawing that scales to whatever box it's put in."""
    rows = matrix(text)
    side = len(rows) + 2 * quiet
    runs = []
    for r, row in enumerate(rows):
        c = 0
        for is_dark, group in itertools.groupby(row):
            width = len(list(group))
            if is_dark:
                runs.append(f"M{c + quiet} {r + quiet}h{width}v1h-{width}z")
            c += width
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="#fff"/><path d="{"".join(runs)}" fill="#000"/></svg>')
