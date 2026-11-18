#!/usr/bin/env python3
"""HEXA POWER のランチャー用アイコン: 光る青い六角形に稲妻
usage: python3 make_icon.py OUT.png [SIZE | REFERENCE.png]"""
import math
import sys

from PIL import Image, ImageDraw, ImageFilter


def hexpts(cx, cy, r):
    return [(cx + r * math.cos(math.radians(60 * i + 30)), cy + r * math.sin(math.radians(60 * i + 30)))
            for i in range(6)]


def make(size):
    s = size * 4
    u = s / 100
    blue = (70, 190, 255, 255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pts = hexpts(50 * u, 50 * u, 44 * u)
    ImageDraw.Draw(glow).line(pts + [pts[0]], fill=(70, 190, 255, 200), width=int(10 * u))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4 * u)))
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=(14, 20, 28, 255))
    d.line(pts + [pts[0]], fill=blue, width=int(4 * u), joint="curve")
    bolt = [(56, 18), (32, 54), (48, 54), (42, 82), (68, 44), (52, 44), (60, 18)]
    d.polygon([(x * u, y * u) for x, y in bolt], fill=(255, 214, 90, 255))
    return img.resize((size, size), Image.LANCZOS)


def main():
    out, size = sys.argv[1], 64
    if len(sys.argv) > 2:
        arg = sys.argv[2]
        if arg.lower().endswith(".png"):
            try:
                size = Image.open(arg).size[0]
            except OSError:
                pass
        else:
            size = int(arg)
    make(size).save(out)
    print(f"icon {size}x{size} -> {out}")


if __name__ == "__main__":
    main()
