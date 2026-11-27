"""產生 PWA 圖示（site/icons/）。改了設計才需要重跑：python scripts/make_icons.py

需要 Pillow（pip install pillow）；它不是專案的相依套件，CI 也不會執行這個檔案。

圖示只用簡單的幾何圖形和 DejaVu Sans Bold 字型，沒有任何外部素材。
maskable 版本的內容縮在中間 80% 的安全區內，Android 裁成圓形或圓角時不會切到字。
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLUE = (47, 95, 179)  # 與 style.css 的 --blue 相同
WHITE = (255, 255, 255)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
OUT = Path(__file__).resolve().parent.parent / "site" / "icons"


def draw(size: int, content_ratio: float) -> Image.Image:
    img = Image.new("RGB", (size, size), BLUE)
    d = ImageDraw.Draw(img)
    box = size * content_ratio
    font = ImageFont.truetype(FONT, int(box * 0.42))
    text = "FX"
    left, t, r, b = d.textbbox((0, 0), text, font=font)
    x = (size - (r - left)) / 2 - left
    y = size * 0.42 - (b - t) / 2 - t
    d.text((x, y), text, font=font, fill=WHITE)
    # 文字下方一條向上的折線，代表匯率走勢
    w = box * 0.62
    x0, y0 = (size - w) / 2, size * 0.5 + box * 0.3
    pts = [
        (x0, y0),
        (x0 + w * 0.35, y0 - box * 0.08),
        (x0 + w * 0.6, y0 - box * 0.02),
        (x0 + w, y0 - box * 0.14),
    ]
    d.line(pts, fill=WHITE, width=max(2, int(size * 0.03)), joint="curve")
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    specs = {
        "icon-192.png": (192, 1.0),
        "icon-512.png": (512, 1.0),
        "icon-maskable-512.png": (512, 0.8),
        "apple-touch-icon.png": (180, 1.0),  # iOS 自己會加圓角，不用透明背景
    }
    for name, (size, ratio) in specs.items():
        draw(size, ratio).save(OUT / name, optimize=True)
        print(OUT / name)


if __name__ == "__main__":
    main()
