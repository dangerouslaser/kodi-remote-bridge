"""Draw the app icons (a remote with signal arcs) into src/."""
from pathlib import Path

from PIL import Image, ImageDraw

SRC = Path(__file__).resolve().parent.parent / "src"


def draw(size: int) -> Image.Image:
    s = 4  # supersample
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=n * 0.2, fill=(18, 25, 31, 255))
    blue = (23, 178, 231, 255)
    # remote body
    w, h = n * 0.26, n * 0.5
    x0, y0 = n * 0.5 - w / 2, n * 0.38
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=w * 0.45, outline=blue, width=int(n * 0.045))
    # OK button
    r = w * 0.2
    cx, cy = n * 0.5, y0 + h * 0.3
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=blue)
    # signal arcs above
    for i, rad in enumerate((n * 0.13, n * 0.22)):
        box = (n * 0.5 - rad, n * 0.3 - rad, n * 0.5 + rad, n * 0.3 + rad)
        d.arc(box, start=225, end=315, fill=blue, width=int(n * 0.04))
    return img.resize((size, size), Image.LANCZOS)


draw(80).save(SRC / "icon.png")
draw(130).save(SRC / "largeIcon.png")
draw(512).save(SRC.parent / "icon-512.png")
