"""Create Piper Reader's multi-resolution Windows application icon."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "build_assets" / "piper_reader.ico"
CANVAS_SIZE = 1024


def _scaled_points(points: list[tuple[float, float]], scale: int) -> list[tuple[int, int]]:
    return [(round(x * scale), round(y * scale)) for x, y in points]


def create_icon() -> Path:
    """Render a clean feather-on-blue icon and save all Windows icon sizes."""

    supersample = 4
    size = CANVAS_SIZE * supersample
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = 72 * supersample
    radius = 224 * supersample
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=radius,
        fill=(35, 92, 224, 255),
    )
    draw.rounded_rectangle(
        (margin + 20 * supersample, margin + 20 * supersample,
         size - margin - 20 * supersample, size - margin - 20 * supersample),
        radius=radius - 20 * supersample,
        outline=(107, 165, 255, 190),
        width=12 * supersample,
    )

    feather = _scaled_points(
        [
            (730, 210), (650, 218), (566, 264), (476, 344), (390, 446),
            (327, 555), (295, 652), (304, 733), (353, 776), (428, 767),
            (519, 710), (602, 632), (668, 539), (716, 444), (748, 354),
            (761, 281),
        ],
        supersample,
    )
    draw.polygon(feather, fill=(248, 251, 255, 255))

    shaft = _scaled_points([(314, 788), (696, 302)], supersample)
    draw.line(shaft, fill=(16, 53, 137, 255), width=28 * supersample)
    draw.line(shaft, fill=(119, 166, 255, 255), width=8 * supersample)

    for start, end in (
        ((370, 684), (514, 642)),
        ((405, 613), (570, 560)),
        ((450, 536), (622, 474)),
        ((508, 454), (674, 393)),
        ((575, 369), (712, 319)),
        ((439, 704), (420, 585)),
        ((521, 645), (494, 506)),
        ((598, 568), (565, 422)),
        ((661, 485), (631, 350)),
    ):
        draw.line(
            _scaled_points([start, end], supersample),
            fill=(35, 92, 224, 255),
            width=18 * supersample,
        )

    image = image.resize((CANVAS_SIZE, CANVAS_SIZE), Image.Resampling.LANCZOS)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        OUTPUT_PATH,
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
               (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return OUTPUT_PATH


if __name__ == "__main__":
    print(create_icon())
