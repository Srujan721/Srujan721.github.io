"""Make the web-size copies of the site's images (assets/img/) from the full-size originals.

The page shows the photo at 212 px, the DARTS icon at 64 px and the education logos at
58 px (CSS pixels). Screens draw 1 to 3 device pixels per CSS pixel, so each image is
written at 1x, 1.25x, 1.5x, 1.75x, 2x, 2.5x and 3x as WebP; the browser picks the copy
that matches the screen, so the image is shown pixel for pixel without a second resize.
One 2x JPEG/PNG is kept for browsers without WebP. The originals are not modified
(photo.jpg is also the link-preview image).

After replacing an original (keep its file name), run from the site folder:
    python tools/optimize_images.py
Needs Python 3 and Pillow (pip install pillow).
"""
import math
import os
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'assets', 'img')

# name: (original file, displayed width in CSS px, 'photo' = lossy WebP q95 / 'graphic' = lossless)
IMAGES = {
    'photo':     ('photo.jpg',     212, 'photo'),
    'darts':     ('DARTS_1.png',    64, 'graphic'),
    'iitr':      ('iitr.jpg',       58, 'graphic'),
    'nitt':      ('nitt.png',       58, 'graphic'),
    'chaitanya': ('chaitanya.jpg',  58, 'graphic'),
    'stbem':     ('stbem.jpg',      58, 'graphic'),
}
SCALES = (1, 1.25, 1.5, 1.75, 2, 2.5, 3)


def web_widths(css_w, full_w=None):
    # file names must match the srcset lists in index.html, so they do not depend on the original's size
    return sorted({math.ceil(css_w * s - 1e-9) for s in SCALES})


def resized(im, w):
    h = round(im.height * w / im.width)
    return im if (w, h) == im.size else im.resize((w, h), Image.LANCZOS)


def save_webp(im, path, kind):
    if kind == 'photo' and im.mode != 'RGBA':
        im.convert('RGB').save(path, 'WEBP', quality=95, method=6)
    else:                                   # logos, icons: exact colours and edges
        im.save(path, 'WEBP', lossless=True, quality=100, method=6, exact=True)


def save_fallback(im, path, kind):
    if path.endswith('.jpg'):
        im.convert('RGB').save(path, 'JPEG', quality=93, subsampling=0, optimize=True, progressive=True)
    else:
        im.save(path, 'PNG', optimize=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (src, css_w, kind) in IMAGES.items():
        im = Image.open(os.path.join(ROOT, src))
        im.load()
        has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
        im = im.convert('RGBA' if has_alpha else 'RGB')
        widths = web_widths(css_w, im.width)
        for w in widths:
            save_webp(resized(im, w), os.path.join(OUT, f'{name}-{w}.webp'), kind)
        ext = 'jpg' if (kind == 'photo' and not has_alpha) else 'png'
        fw = round(css_w * 2)
        save_fallback(resized(im, fw), os.path.join(OUT, f'{name}-{fw}.{ext}'), kind)
        print(f'{src:15s} {im.width}x{im.height} -> widths {widths}, fallback {name}-{fw}.{ext}')


if __name__ == '__main__':
    main()
