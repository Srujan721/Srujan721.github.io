"""Make the Gallery's web-size photos and its photo list (assets/gallery/gallery.json).

Photos are uploaded to the gallery/ folder at any size (JPEG, PNG, WebP, TIFF, or HEIC from an
iPhone). A photo in a subfolder, e.g. gallery/Conferences/IMG_1234.jpg, joins the album
"Conferences". For each photo this script
  - turns it upright (phones often store photos sideways with a rotation flag),
  - converts it to sRGB and drops all metadata (GPS location, camera, time stamps),
  - writes WebP copies 400, 720 and 1200 px wide and one up to 2000 px on the long side
    (a small original is never enlarged) as assets/gallery/<id>-<width>.webp,
and lists every photo, newest first, in assets/gallery/gallery.json, which the page reads.

Captions, places, dates and albums come from gallery/captions.yml (all optional). A photo's
date is taken from captions.yml, else from the camera (EXIF), else from a date in the file name
(IMG_20251203_142233.jpg, WhatsApp Image 2025-12-03 ...); photos without a date go last.
Unchanged photos are not processed again (the list keeps a fingerprint of each upload), and the
copies of photos deleted from gallery/ are removed.

The GitHub Action .github/workflows/gallery.yml runs this after every upload to gallery/.
To run it yourself from the site folder:
    pip install pillow pyyaml pillow-heif        (pillow-heif is only needed for HEIC)
    python tools/build_gallery.py
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import unicodedata

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

try:
    import yaml
except ImportError:          # only needed when gallery/captions.yml has entries
    yaml = None

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'gallery')
CAPTIONS = os.path.join(SRC, 'captions.yml')
OUT = os.path.join(ROOT, 'assets', 'gallery')
LIST = os.path.join(OUT, 'gallery.json')

WIDTHS = (400, 720, 1200)    # grid tiles on phones and desktops (1x to 3x screens)
LONG_EDGE = 2000             # largest copy, for the full-screen viewer
QUALITY = 82                 # WebP quality
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.heic', '.heif'}
FIELDS = {'file', 'caption', 'place', 'date', 'album', 'alt'}
Image.MAX_IMAGE_PIXELS = 250_000_000     # phone sensors up to 200 MP

IN_ACTIONS = os.environ.get('GITHUB_ACTIONS') == 'true'


def warn(msg):
    # In GitHub Actions the message is also shown on the run's summary page
    print(('::warning::' if IN_ACTIONS else 'WARNING: ') + msg)


def fail(msg):
    print(('::error::' if IN_ACTIONS else 'ERROR: ') + msg)
    sys.exit(1)


def fingerprint(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()[:20]


def valid_date(y, m=None, d=None):
    try:
        dt.date(int(y), int(m or 1), int(d or 1))
        return 1980 <= int(y) <= 2100
    except ValueError:
        return False


def norm_date(value):
    """'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' from captions.yml (YAML reads 2024-12-03 as a date)."""
    if value in (None, ''):
        return ''
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime('%Y-%m-%d')
    m = re.fullmatch(r'(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?', str(value).strip())
    if not m or not valid_date(*m.groups()):
        raise ValueError(value)
    y, mo, d = m.groups()
    return '-'.join([y] + [f'{int(x):02d}' for x in (mo, d) if x])


def exif_date(im):
    try:
        ex = im.getexif()
        raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)    # DateTimeOriginal, else DateTime
        m = re.match(r'(\d{4}):(\d{2}):(\d{2})', str(raw or ''))
        if m and valid_date(*m.groups()):
            return '-'.join(m.groups())
    except Exception:
        pass
    return ''


def name_date(name):
    m = re.search(r'(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)', name)
    if m and valid_date(*m.groups()):
        return '-'.join(m.groups())
    m = re.search(r'(?<!\d)((?:19|20)\d\d)[-_.](0[1-9]|1[0-2])(?!\d)', name)
    if m and valid_date(*m.groups()):
        return '-'.join(m.groups())
    return ''


def slug(rel):
    stem = os.path.splitext(rel)[0]
    s = unicodedata.normalize('NFKD', stem).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower()[:80].strip('-') or 'photo'


def load_captions():
    """{lower-case file name: entry} from gallery/captions.yml; stops on a format error."""
    if not os.path.exists(CAPTIONS):
        return {}
    text = open(CAPTIONS, encoding='utf-8-sig').read()
    if not re.search(r'^\s*-', text, re.M):         # only comments so far
        return {}
    if yaml is None:
        fail('gallery/captions.yml has entries but PyYAML is not installed (pip install pyyaml).')

    class TextDates(yaml.SafeLoader):
        """Safe loader that keeps dates as text, so a wrong date affects only its own photo."""
    TextDates.yaml_implicit_resolvers = {
        ch: [r for r in rs if r[0] != 'tag:yaml.org,2002:timestamp']
        for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    try:
        data = yaml.load(text, Loader=TextDates) or []
    except yaml.YAMLError as e:
        fail(f'gallery/captions.yml could not be read, so nothing was changed.\n{e}\n'
             'Tip: put a caption in double quotes when it contains a colon (:) or a #.')
    if not isinstance(data, list):
        fail('gallery/captions.yml must be a list: each photo starts with a line "- file: NAME".')
    out = {}
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict) or not item.get('file'):
            fail(f'gallery/captions.yml, entry {n}: every entry needs a "file:" line.')
        key = str(item['file']).strip().replace('\\', '/').lower()
        extra = sorted(set(item) - FIELDS)
        if extra:
            warn(f'gallery/captions.yml, {item["file"]}: unknown field(s) {", ".join(map(str, extra))} ignored.')
        if key in out:
            warn(f'gallery/captions.yml lists {item["file"]} twice; the later entry is used.')
        out[key] = item
    return out


def caption_keys(rel):
    """Names under which captions.yml may refer to a photo: with or without folder and extension."""
    base = os.path.basename(rel)
    return [k.lower() for k in (rel, base, os.path.splitext(rel)[0], os.path.splitext(base)[0])]


def find_photos():
    """Relative paths ('IMG_1.jpg', 'Conferences/IMG_2.jpg') of the photos in gallery/."""
    found = []
    for folder, dirs, files in os.walk(SRC):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in sorted(files):
            if name.startswith('.') or os.path.splitext(name)[1].lower() not in PHOTO_EXTS:
                continue
            if os.path.splitext(name)[1].lower() in ('.heic', '.heif') and not HEIF:
                warn(f'{name}: HEIC photos need pillow-heif (pip install pillow-heif); skipped.')
                continue
            found.append(os.path.relpath(os.path.join(folder, name), SRC).replace(os.sep, '/'))
    return found


def to_srgb(im):
    icc = im.info.get('icc_profile')
    if not icc:
        return im
    try:
        if im.mode not in ('RGB', 'RGBA', 'L', 'CMYK'):
            im = im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
        mode = 'RGBA' if im.mode == 'RGBA' else 'RGB'
        return ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                         ImageCms.createProfile('sRGB'), outputMode=mode)
    except Exception as e:                           # unusual profile: keep the colours as stored
        warn(f'colour profile not converted ({e}); colours may look slightly different.')
        return im


def make_copies(path, pid):
    """Writes the web copies of one photo; returns its size, widths and camera date."""
    with Image.open(path) as im:
        taken = exif_date(im)
        w0, h0 = im.size
        s = min(1.0, LONG_EDGE / max(w0, h0))
        im.draft('RGB', (max(1, round(w0 * s)), max(1, round(h0 * s))))   # faster JPEG decoding
        im = ImageOps.exif_transpose(im)
        im = to_srgb(im)
        if im.mode in ('RGBA', 'LA', 'PA') or (im.mode == 'P' and 'transparency' in im.info):
            im = im.convert('RGBA')
            flat = Image.new('RGB', im.size, (255, 255, 255))
            flat.paste(im, mask=im.getchannel('A'))
            im = flat
        else:
            im = im.convert('RGB')
        im.info = {}                                  # no EXIF, XMP or ICC in the copies
        w, h = im.size
        s = min(1.0, LONG_EDGE / max(w, h))
        top = max(1, round(w * s))
        widths = sorted({x for x in WIDTHS if x < top * 0.85} | {top})
        for x in widths:
            y = max(1, round(h * x / w))
            out = im if (x, y) == (w, h) else im.resize((x, y), Image.LANCZOS, reducing_gap=3.0)
            out.save(os.path.join(OUT, f'{pid}-{x}.webp'), 'WEBP', quality=QUALITY, method=6, exif=b'', xmp=b'')
        return {'w': top, 'h': max(1, round(h * top / w)), 'widths': widths, 'taken': taken}


def main():
    os.makedirs(SRC, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    captions = load_captions()
    try:
        old = {p['file']: p for p in json.load(open(LIST, encoding='utf-8')).get('photos', [])}
    except (OSError, ValueError, AttributeError):
        old = {}

    photos = find_photos()
    # ids: a photo keeps the id it had before, so its web addresses stay the same
    taken_ids, ids = set(), {}
    for rel in photos:
        if rel in old and old[rel].get('id') and old[rel]['id'] not in taken_ids:
            ids[rel] = old[rel]['id']
            taken_ids.add(ids[rel])
    for rel in photos:
        if rel not in ids:
            base, k = slug(rel), 2
            pid = base
            while pid in taken_ids:
                pid, k = f'{base}-{k}', k + 1
            ids[rel] = pid
            taken_ids.add(pid)

    entries, made, kept, failed = [], 0, 0, 0
    for rel in photos:
        path, pid = os.path.join(SRC, rel), ids[rel]
        fp = fingerprint(path)
        prev = old.get(rel) or {}
        unchanged = (prev.get('sha') == fp and prev.get('id') == pid and bool(prev.get('widths'))
                     and all(os.path.exists(os.path.join(OUT, f'{pid}-{x}.webp')) for x in prev['widths']))
        if unchanged:
            info = {k: prev[k] for k in ('w', 'h', 'widths')}
            info['taken'] = prev.get('taken', '')
            kept += 1
        else:
            try:
                info = make_copies(path, pid)
                made += 1
                print(f'  {rel}: {info["w"]}x{info["h"]}, widths {info["widths"]}')
            except Exception as e:
                why = 'not an image this script can read' if isinstance(e, UnidentifiedImageError) else str(e)
                warn(f'{rel} could not be read as a photo ({why}); skipped.')
                failed += 1
                continue

        meta = next((captions[k] for k in caption_keys(rel) if k in captions), {})
        try:
            date = norm_date(meta.get('date'))
        except ValueError:
            warn(f'gallery/captions.yml, {rel}: date "{meta.get("date")}" is not YYYY-MM-DD or YYYY-MM; ignored.')
            date = ''
        date = date or info['taken'] or name_date(os.path.basename(rel))
        album = str(meta.get('album') or (rel.split('/')[0] if '/' in rel else '')).strip()
        entry = {'id': pid, 'file': rel, 'sha': fp, 'w': info['w'], 'h': info['h'], 'widths': info['widths'],
                 'taken': info['taken'], 'date': date}
        for key, val in (('caption', meta.get('caption')), ('place', meta.get('place')),
                         ('album', album), ('alt', meta.get('alt'))):
            val = re.sub(r'\s+', ' ', str(val or '')).strip()
            if val:
                entry[key] = val
        entries.append(entry)

    used = {k for e in entries for k in caption_keys(e['file'])}
    for key in captions:
        if key not in used:
            warn(f'gallery/captions.yml names "{captions[key]["file"]}", but no such photo is in gallery/.')

    # newest first; photos without a date last, by name
    entries.sort(key=lambda e: e['file'].lower())
    entries.sort(key=lambda e: e['date'], reverse=True)

    # remove copies that no listed photo uses any more
    wanted = {f'{e["id"]}-{x}.webp' for e in entries for x in e['widths']}
    removed = 0
    for name in os.listdir(OUT):
        if re.fullmatch(r'[a-z0-9-]+-\d+\.webp', name) and name not in wanted:
            os.remove(os.path.join(OUT, name))
            removed += 1

    with open(LIST, 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'photos': entries}, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print(f'Gallery: {len(entries)} photo(s) listed; {made} processed, {kept} unchanged, '
          f'{failed} skipped, {removed} old file(s) removed.')


if __name__ == '__main__':
    main()
