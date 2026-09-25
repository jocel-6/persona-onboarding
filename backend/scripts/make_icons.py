"""App icons for the installable web app (#12): the call-screen orb, in Persona's blue-gray.

    .venv/bin/python scripts/make_icons.py   ->  frontend/public/icons/*.png
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[2] / "frontend" / "public" / "icons"
LIGHT, DARK = (147, 168, 176), (95, 120, 128)  # the band's #93a8b0 -> #5f7880


def icon(size: int, *, maskable: bool = False) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):  # diagonal gradient, like the orb
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(LIGHT[i] + (DARK[i] - LIGHT[i]) * t) for i in range(3))
    # soft highlight, top-left
    glow = Image.new("L", (size, size), 0)
    ImageDraw.Draw(glow).ellipse((-size * 0.2, -size * 0.25, size * 0.7, size * 0.55), fill=90)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    img = Image.composite(Image.new("RGB", (size, size), (255, 255, 255)), img, glow)
    # the letter, kept inside the maskable safe zone
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", int(size * (0.42 if maskable else 0.5)), index=1)
    box = d.textbbox((0, 0), "P", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), "P", font=font, fill=(255, 255, 255))
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        icon(size).save(OUT / f"icon-{size}.png")
    icon(512, maskable=True).save(OUT / "icon-maskable-512.png")
    icon(180).save(OUT / "apple-touch-icon.png")
    print("wrote", sorted(p.name for p in OUT.iterdir()))
