"""Generate runtime PNG and ICO assets from the canonical light icon source."""

from collections import deque
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIRECTORY = PROJECT_ROOT / "assets"
SOURCE_PATH = ASSET_DIRECTORY / "app_icon_source.png"
PNG_PATH = ASSET_DIRECTORY / "app_icon.png"
HEADER_PATH = ASSET_DIRECTORY / "app_icon_64.png"
ICO_PATH = ASSET_DIRECTORY / "app_icon.ico"


def is_connected_background(red: int, green: int, blue: int) -> bool:
    brightest = max(red, green, blue)
    darkest = min(red, green, blue)
    return brightest <= 16 and brightest - darkest <= 10


def remove_connected_black_background(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    visited = bytearray(width * height)
    queue = deque()

    def enqueue(x: int, y: int) -> None:
        index = y * width + x
        if visited[index]:
            return
        red, green, blue, _alpha = pixels[x, y]
        if not is_connected_background(red, green, blue):
            return
        visited[index] = 1
        queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while queue:
        x, y = queue.popleft()
        if x > 0:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y > 0:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)

    for y in range(height):
        for x in range(width):
            if visited[y * width + x]:
                red, green, blue, _alpha = pixels[x, y]
                pixels[x, y] = (red, green, blue, 0)

    return rgba


def main() -> None:
    if not SOURCE_PATH.is_file():
        raise SystemExit("Missing icon source: {}".format(SOURCE_PATH))

    ASSET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with Image.open(SOURCE_PATH) as source:
        prepared = remove_connected_black_background(source)

    icon = prepared.resize((512, 512), Image.Resampling.LANCZOS)
    icon.save(PNG_PATH, format="PNG", optimize=True)
    icon.resize((64, 64), Image.Resampling.LANCZOS).save(
        HEADER_PATH,
        format="PNG",
        optimize=True,
    )
    icon.save(
        ICO_PATH,
        format="ICO",
        sizes=[
            (16, 16),
            (20, 20),
            (24, 24),
            (32, 32),
            (40, 40),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )

    print("Prepared {}".format(PNG_PATH))
    print("Prepared {}".format(HEADER_PATH))
    print("Prepared {}".format(ICO_PATH))


if __name__ == "__main__":
    main()
