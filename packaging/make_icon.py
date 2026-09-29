"""Generate Dub Checker's icon: dubchecker/assets/icon.ico plus 64 and 256 px PNGs.

Design: a violet-to-blue rounded square; a translucent white speech bubble with
"あ" behind a solid white bubble with "EN". Sizes of 32 px and below use a
simplified variant without text so they stay crisp.

Needs Pillow (not part of the app's requirements):  pip install pillow
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "dubchecker" / "assets"
MASTER = 1024
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
VIOLET = (124, 58, 237)
BLUE = (37, 99, 235)
DEEP_VIOLET = (76, 29, 149)

JAPANESE_FONTS = ("C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc", "C:/Windows/Fonts/msgothic.ttc",
                  "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                  "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc")
LATIN_FONTS = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
               "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def load_font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | None:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return None


def gradient(size: int) -> Image.Image:
    """A diagonal violet (top left) to blue (bottom right) fill."""
    vertical = Image.linear_gradient("L")      # black at the top, white at the bottom
    horizontal = vertical.rotate(90)           # black on the left, white on the right
    mask = ImageChops.add(vertical, horizontal, scale=2.0).resize((size, size), Image.BICUBIC)
    return Image.composite(Image.new("RGB", (size, size), BLUE), Image.new("RGB", (size, size), VIOLET), mask)


def bubble(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], tail_left: bool,
           fill: tuple[int, ...]) -> None:
    x0, y0, x1, y1 = box
    radius = (y1 - y0) * 0.32
    draw.rounded_rectangle(box, radius=radius, fill=fill)
    w = x1 - x0
    if tail_left:
        tail = [(x0 + w * 0.16, y1 - 2), (x0 + w * 0.06, y1 + (y1 - y0) * 0.30), (x0 + w * 0.38, y1 - 2)]
    else:
        tail = [(x1 - w * 0.16, y1 - 2), (x1 - w * 0.06, y1 + (y1 - y0) * 0.30), (x1 - w * 0.38, y1 - 2)]
    draw.polygon(tail, fill=fill)


def centered_text(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], text: str,
                  font: ImageFont.FreeTypeFont, fill: tuple[int, ...]) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    draw.text((cx - (left + right) / 2, cy - (top + bottom) / 2), text, font=font, fill=fill)


def render(simple: bool) -> Image.Image:
    s = MASTER
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    mask = Image.new("L", (s, s), 0)
    margin = s * 0.03
    ImageDraw.Draw(mask).rounded_rectangle((margin, margin, s - margin, s - margin), radius=s * 0.22, fill=255)
    canvas.paste(gradient(s), (0, 0), mask)

    back_box = (s * 0.13, s * 0.17, s * 0.63, s * 0.53) if not simple else (s * 0.12, s * 0.14, s * 0.66, s * 0.54)
    front_box = (s * 0.37, s * 0.43, s * 0.87, s * 0.79) if not simple else (s * 0.34, s * 0.42, s * 0.88, s * 0.82)

    back = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bubble(ImageDraw.Draw(back), back_box, tail_left=True, fill=(255, 255, 255, 105))
    if not simple:
        font = load_font(JAPANESE_FONTS, int(s * 0.27))
        if font is not None:
            centered_text(ImageDraw.Draw(back), (back_box[0], back_box[1], back_box[2] - s * 0.1, back_box[3]),
                          "あ", font, (255, 255, 255, 235))
    canvas = Image.alpha_composite(canvas, back)

    front = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(front)
    bubble(draw, front_box, tail_left=False, fill=(255, 255, 255, 255))
    if not simple:
        font = load_font(LATIN_FONTS, int(s * 0.21))
        if font is not None:
            centered_text(draw, front_box, "EN", font, DEEP_VIOLET + (255,))
    return Image.alpha_composite(canvas, front)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    full, simple = render(simple=False), render(simple=True)
    images = [(simple if size <= 32 else full).resize((size, size), Image.LANCZOS) for size in ICO_SIZES]
    images[-1].save(ASSETS / "icon.ico", format="ICO", sizes=[(n, n) for n in ICO_SIZES], append_images=images[:-1])
    full.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon_256.png")
    full.resize((64, 64), Image.LANCZOS).save(ASSETS / "icon_64.png")
    print(f"Icons written to {ASSETS}")


if __name__ == "__main__":
    main()
