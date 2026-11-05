"""Draws the garden's art and writes it into the page's stylesheet: the strip of grass along the top
bar and the tiles of stones for the ground (light, dark and pink). Everything is drawn here from
fixed random seeds, so running it again gives the same pictures.

    python3 tools/garden_art.py

It replaces what's between the "art:start" and "art:end" comments in cardfarm/ui/index.html."""
import math
import os
import random
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "cardfarm", "ui", "index.html")
START, END = "/* art:start (tools/garden_art.py) */", "/* art:end */"


def uri(svg):
    return "url(\"data:image/svg+xml," + urllib.parse.quote(svg, safe=" =:/'(),.-") + "\")"


def grass(width=180, height=18, seed=11):
    """Blades of grass, two layers deep, wrapping at the edges so the strip repeats without a seam."""
    rnd, paths = random.Random(seed), []
    for layer, colours in ((0, ["#3f7426", "#4a8a2c"]), (1, ["#5f9f36", "#6fb441", "#7cc44f"])):
        x = -4
        while x < width + 4:
            w, h = rnd.uniform(3, 6), rnd.uniform(7, 17) if layer else rnd.uniform(10, 18)
            lean, colour = rnd.uniform(-4, 4), rnd.choice(colours)
            for bx in (x - width, x, x + width):
                if -6 < bx < width + 6:
                    paths.append(f"<path d='M{bx:.1f} {height} Q{bx + w / 2 + lean / 2:.1f} {height - h * .55:.1f} "
                                 f"{bx + w / 2 + lean:.1f} {height - h:.1f} Q{bx + w * .6 + lean / 3:.1f} {height - h * .5:.1f} "
                                 f"{bx + w:.1f} {height}Z' fill='{colour}'/>")
            x += rnd.uniform(2.5, 5.5)
    return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"
            + "".join(paths) + "</svg>")


def stones(gap, tones, light, shade, grain, size=352, seed=5):
    """Rounded stones on a jittered grid over darker ground, each lit from the top left, with a fine
    grit on top. Stones crossing an edge are drawn on the other side too, so the tile repeats."""
    rnd, out, step = random.Random(seed), [], 44
    for gy in range(0, size, step):
        for gx in range(0, size, step):
            cx, cy, r = gx + rnd.uniform(6, step - 6), gy + rnd.uniform(6, step - 6), rnd.uniform(21, 29)
            n = rnd.randint(7, 10)
            ring = [(cx + math.cos(a) * r * rnd.uniform(.78, 1.12), cy + math.sin(a) * r * rnd.uniform(.72, 1.05))
                    for a in sorted(rnd.uniform(0, 2 * math.pi) for _ in range(n))]
            mids = [((ring[k][0] + ring[(k + 1) % n][0]) / 2, (ring[k][1] + ring[(k + 1) % n][1]) / 2) for k in range(n)]
            tone = rnd.choice(tones)
            for dx in (-size, 0, size):
                for dy in (-size, 0, size):
                    if not (-r * 1.3 < cx + dx < size + r * 1.3 and -r * 1.3 < cy + dy < size + r * 1.3):
                        continue
                    d = f"M{mids[-1][0] + dx:.1f} {mids[-1][1] + dy:.1f}" + "".join(
                        f"Q{ring[k][0] + dx:.1f} {ring[k][1] + dy:.1f} {mids[k][0] + dx:.1f} {mids[k][1] + dy:.1f}" for k in range(n))
                    out.append(f"<path d='{d}Z' fill='{tone}'/><path d='{d}Z' fill='url(#l)'/>")
    return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}'>"
            "<defs><linearGradient id='l' x1='0' y1='0' x2='1' y2='1'>"
            f"<stop offset='0' stop-color='{light}' stop-opacity='.35'/><stop offset='.55' stop-color='{light}' stop-opacity='0'/>"
            f"<stop offset='1' stop-color='{shade}' stop-opacity='.45'/></linearGradient>"
            "<filter id='g' x='0' y='0' width='100%' height='100%'><feTurbulence type='fractalNoise' baseFrequency='0.8' "
            "numOctaves='2' seed='9' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.5 1'/>"
            f"</filter></defs><rect width='{size}' height='{size}' fill='{gap}'/>" + "".join(out)
            + f"<rect width='{size}' height='{size}' filter='url(#g)' opacity='{grain}'/></svg>")


def art():
    rocks = {
        "light": stones("#a8916c", ["#d8c4a0", "#cfb994", "#dccaa8", "#c9b28c", "#d3bf9b"], "#fff8e8", "#6d5a40", ".12"),
        "dark": stones("#241b14", ["#4a3a2c", "#523f2f", "#45362a", "#5a4634", "#3f3227"], "#d9b48c", "#120c08", ".25"),
        "pink": stones("#d9aab4", ["#f6dde2", "#f2d3d9", "#efcbd2", "#f8e4e8", "#ecc4cc"], "#fff7f9", "#a06a78", ".10"),
    }
    lines = [f"  --grass: {uri(grass())};"] + [f"  --rock-{k}: {uri(v)};" for k, v in rocks.items()]
    return f"{START}\n:root {{\n" + "\n".join(lines) + f"\n}}\n{END}"


def main():
    page = open(PAGE, encoding="utf-8").read()
    i, j = page.index(START), page.index(END) + len(END)
    open(PAGE, "w", encoding="utf-8").write(page[:i] + art() + page[j:])
    print(f"wrote the garden's art into {os.path.relpath(PAGE, ROOT)}")


if __name__ == "__main__":
    main()
