"""Generate synthetic shelf images for the three reproducible reader scenarios."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BOOKS = [
    ("Python 编程", "第3版", "TP311.56/18", "#396557"),
    ("算法导论", "第4版", "TP301.6/11", "#324963"),
    ("数据结构", "第3版", "TP311.12/03", "#93654A"),
    ("计算机网络", "第8版", "TP393/08", "#48657D"),
    ("数据库系统概论", "第6版", "TP311.13/06", "#695470"),
    ("软件工程", "第10版", "TP311.5/10", "#596B42"),
]


def render(path, font_path, books):
    height = 660
    widths = [190, 170, 178, 180, 226, 171]
    canvas = Image.new("RGB", (sum(widths), height))
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.truetype(str(font_path), 34)
    detail_font = ImageFont.truetype(str(font_path), 24)
    cursor = 0
    for index, (width, (title, edition, call, color)) in enumerate(zip(widths, books)):
        end = cursor + width
        draw.rectangle((cursor, 0, end, height), fill=color)
        draw.rectangle((cursor, 0, cursor+5, height), fill="#24352e")
        draw.rectangle((end-4, 0, end, height), fill="#2c392c")
        start_y = 67
        draw.line((cursor+18, 46, end-18, 46), fill="#c8d0bd", width=2)
        if len(title) > 5 and " " not in title:
            for line in (title[:4], title[4:]):
                draw.text((cursor+17, start_y), line, font=title_font, fill="#fff5de")
                start_y += 47
        else:
            display = "Python\n编程" if title.startswith("Python") else title
            draw.multiline_text((cursor+15, start_y), display, font=title_font, fill="#fff5de", spacing=4)
            start_y += 96 if "\n" in display else 48
        draw.text((cursor+17, start_y+28), edition, font=detail_font, fill="#fff5de")
        sticker = height - 172 + index % 3 * 29
        draw.line((cursor+18, sticker-20, end-18, sticker-20), fill="#d3ddca", width=2)
        draw.rectangle((cursor+13, sticker, end-13, sticker+46), fill="#e8ddc1")
        draw.text((cursor+17, sticker+9), call, font=ImageFont.truetype(str(font_path), 20), fill="#242723")
        draw.rectangle((cursor, height-23, end, height), fill="#3e3027")
        cursor = end
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, "JPEG", quality=94, optimize=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyhbd.ttc"))
    args = parser.parse_args()
    for slug, alternate in (("target", BOOKS[2]), ("wrong", ("数据结构", "第2版", "TP311.12/02", "#93654A")),
                            ("absent", ("操作系统", "第3版", "TP316/03", "#734c4e"))):
        books = [*BOOKS[:2], alternate, *BOOKS[3:]]
        render(ROOT / "web" / "samples" / (slug + ".jpg"), args.font, books)


if __name__ == "__main__":
    main()
