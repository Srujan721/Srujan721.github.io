"""Make the web-size photos and the list (assets/gallery/gallery.json) for "Photos & Videos".

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

Videos come in two ways:
  - a video file uploaded to gallery/ (MP4, MOV from a phone, WebM, ...): converted with ffmpeg to a
    web MP4 that plays in every browser (H.264 + AAC, at most 1280 px, 30 frames/s) as
    assets/gallery/<id>.mp4, with a cover taken from the video. A file named typing.<ext> or
    touch typing.<ext> (any capitals; spaces, hyphens or underscores allowed, e.g. Touch_Typing.mp4)
    becomes the video of the Touch Typing block; other videos are listed with the photos.
  - a link in gallery/captions.yml ("video:" with a YouTube, Vimeo or Google Drive address): the
    video's cover image is downloaded once (or a photo from gallery/ named in "file:" is used).
The page plays a video only when a visitor presses play.

The GitHub Action .github/workflows/gallery.yml runs this after every upload to gallery/.
To run it yourself from the site folder:
    pip install pillow pyyaml pillow-heif        (pillow-heif is only needed for HEIC)
    python tools/build_gallery.py                (video files also need ffmpeg)
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageCms, ImageOps, ImageStat, UnidentifiedImageError

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
VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv', '.3gp'}
FIELDS = {'file', 'video', 'caption', 'place', 'date', 'album', 'alt'}
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
    """({lower-case file name: photo entry}, [video entries]) from gallery/captions.yml; stops on a format error."""
    if not os.path.exists(CAPTIONS):
        return {}, []
    text = open(CAPTIONS, encoding='utf-8-sig').read()
    if not re.search(r'^\s*-', text, re.M):         # only comments so far
        return {}, []
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
    out, videos = {}, []
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict) or not (item.get('file') or item.get('video')):
            fail(f'gallery/captions.yml, entry {n}: every entry needs a "file:" line (a photo) or a "video:" line (a link).')
        name = item.get('video') or item['file']
        extra = sorted(set(item) - FIELDS)
        if extra:
            warn(f'gallery/captions.yml, {name}: unknown field(s) {", ".join(map(str, extra))} ignored.')
        if item.get('video'):
            videos.append(item)
            continue
        key = str(item['file']).strip().replace('\\', '/').lower()
        if key in out:
            warn(f'gallery/captions.yml lists {item["file"]} twice; the later entry is used.')
        out[key] = item
    return out, videos


def video_link(url):
    """(provider, id, link) for a YouTube, Vimeo or Google Drive link, or a direct link to a video file."""
    u = str(url or '').strip()
    m = re.search(r'(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})', u, re.I)
    if m:
        return 'youtube', m.group(1), 'https://www.youtube.com/watch?v=' + m.group(1)
    m = re.search(r'vimeo\.com/(?:video/)?(\d+)', u, re.I)
    if m:
        return 'vimeo', m.group(1), 'https://vimeo.com/' + m.group(1)
    m = re.search(r'drive\.google\.com/(?:file/d/|open\?id=)([\w-]{10,})', u, re.I)
    if m:
        return 'drive', m.group(1), 'https://drive.google.com/file/d/' + m.group(1) + '/view'
    if re.match(r'^https?://[^\s"\'<>]+\.(mp4|webm|m4v|mov)([?#][^\s"\'<>]*)?$', u, re.I):
        return 'file', '', u
    return None


def fetch(url, limit=15 << 20):
    req = urllib.request.Request(url, headers={'User-Agent': 'srujan721.github.io gallery builder'})
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype, data = r.headers.get('Content-Type', ''), r.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file too large')
    return ctype, data


def video_cover(provider, vid, link):
    """The video's cover image as bytes, or None (the page then shows a plain play button)."""
    try:
        if provider == 'youtube':
            for name in ('maxresdefault', 'sddefault', 'hqdefault'):   # the large ones exist only for HD videos
                try:
                    ctype, data = fetch(f'https://i.ytimg.com/vi/{vid}/{name}.jpg')
                except urllib.error.HTTPError:
                    continue
                if ctype.startswith('image/') and len(data) > 2000:
                    return data
        elif provider == 'vimeo':
            _, data = fetch('https://vimeo.com/api/oembed.json?width=1280&url=' + urllib.parse.quote(link, safe=''))
            thumb = json.loads(data).get('thumbnail_url')
            if thumb:
                ctype, data = fetch(thumb)
                return data if ctype.startswith('image/') else None
        elif provider == 'drive':   # only for files shared with "anyone with the link"
            ctype, data = fetch(f'https://drive.google.com/thumbnail?id={vid}&sz=w1280')
            return data if ctype.startswith('image/') else None
    except Exception as e:
        warn(f'the cover image of {link} could not be downloaded ({e}); its tile shows a plain play button.')
    return None


def without_letterbox(im):
    """YouTube's smaller covers are 4:3 with black bands above and below a 16:9 picture: crop them off."""
    w, h = im.size
    if abs(w / h - 4 / 3) > 0.02:
        return im
    band = round(h * 0.125)
    grey = im.convert('L')
    dark = lambda box: ImageStat.Stat(grey.crop(box)).mean[0] < 24
    if dark((0, 0, w, band - 2)) and dark((0, h - band + 2, w, h)):
        return im.crop((0, band, w, h - band))
    return im


def caption_keys(rel):
    """Names under which captions.yml may refer to a photo: with or without folder and extension."""
    base = os.path.basename(rel)
    return [k.lower() for k in (rel, base, os.path.splitext(rel)[0], os.path.splitext(base)[0])]


def find_videos():
    """Relative paths of the video files in gallery/ (converted by make_video)."""
    found = []
    for folder, dirs, files in os.walk(SRC):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in sorted(files):
            if not name.startswith('.') and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                found.append(os.path.relpath(os.path.join(folder, name), SRC).replace(os.sep, '/'))
    return found


def make_video(path, pid):
    """Converts one uploaded video to assets/gallery/<pid>.mp4 and makes its cover; returns sizes."""
    out = os.path.join(OUT, f'{pid}.mp4')
    tmp = out + '.part.mp4'
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', path,
           '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn', '-map_metadata', '-1',
           '-vf', 'scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2',
           '-fpsmax', '30', '-c:v', 'libx264', '-preset', 'medium', '-crf', '26', '-pix_fmt', 'yuv420p',
           '-profile:v', 'high', '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-movflags', '+faststart', tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError((r.stderr.strip().splitlines() or ['ffmpeg failed'])[-1])
    os.replace(tmp, out)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=width,height:format=duration', '-of', 'json', out], capture_output=True, text=True)
    info = json.loads(probe.stdout or '{}')
    dur = float((info.get('format') or {}).get('duration') or 0)
    with tempfile.TemporaryDirectory() as tmpdir:   # cover: a frame a third of the way in (at most 3 s)
        cover = os.path.join(tmpdir, 'cover.png')
        subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-ss', f'{min(3.0, dur / 3):.2f}', '-i', out,
                        '-frames:v', '1', cover], capture_output=True)
        copies = make_copies(cover, pid) if os.path.exists(cover) else {'w': 16, 'h': 9, 'widths': []}
    return {'w': copies['w'], 'h': copies['h'], 'widths': copies['widths'], 'duration': round(dur, 1),
            'size': os.path.getsize(out)}


def find_photos():
    """Relative paths ('IMG_1.jpg', 'Conferences/IMG_2.jpg') of the photos in gallery/."""
    found = []
    for folder, dirs, files in os.walk(SRC):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if name.startswith('.') or ext not in PHOTO_EXTS:
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


def describe(entry, meta):
    """Adds the optional caption, place, album and alt text from captions.yml to a list entry."""
    folder = entry.pop('album', '')   # a subfolder name; "album:" in captions.yml takes precedence
    for key, val in (('caption', meta.get('caption')), ('place', meta.get('place')),
                     ('album', meta.get('album') or folder), ('alt', meta.get('alt'))):
        val = re.sub(r'\s+', ' ', str(val or '')).strip()
        if val:
            entry[key] = val
    return entry


def date_of(meta, name):
    try:
        return norm_date(meta.get('date'))
    except ValueError:
        warn(f'gallery/captions.yml, {name}: date "{meta.get("date")}" is not YYYY-MM-DD or YYYY-MM; ignored.')
        return ''


def main():
    os.makedirs(SRC, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    captions, videos = load_captions()
    try:
        old = {('video:' + p['url']) if p.get('type') == 'video' and str(p.get('url', '')).startswith('http') else p['file']: p
               for p in json.load(open(LIST, encoding='utf-8')).get('photos', [])}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        old = {}

    # video entries that will be listed: a valid link, and the first entry for each video
    usable, seen, failed = [], set(), 0
    for v in videos:
        found = video_link(v['video'])
        if not found:
            warn(f'gallery/captions.yml: "{v["video"]}" is not a YouTube, Vimeo or Google Drive link, '
                 'or a link to a video file; skipped.')
            failed += 1
        elif found[2] in seen:
            warn(f'gallery/captions.yml lists the video {found[2]} twice; the first entry is used.')
        else:
            seen.add(found[2])
            usable.append((v, found))

    # photos named as a video's cover ("file:" in a video entry) are not listed on their own
    everything = find_photos()
    cover_keys = {str(v['file']).strip().replace('\\', '/').lower() for v, _ in usable if v.get('file')}
    cover_of = {}
    for rel in everything:
        for k in caption_keys(rel):
            if k in cover_keys:
                cover_of.setdefault(k, rel)
    photos = [rel for rel in everything if rel not in cover_of.values()]
    uploads = find_videos()

    # ids: a photo or video keeps the id it had before, so its web addresses stay the same
    taken_ids, ids = set(), {}
    for rel in photos + uploads:
        if rel in old and old[rel].get('id') and old[rel]['id'] not in taken_ids:
            ids[rel] = old[rel]['id']
            taken_ids.add(ids[rel])
    for rel in photos + uploads:
        if rel not in ids:
            base, k = slug(rel), 2
            pid = base
            while pid in taken_ids or pid.startswith('video-'):
                pid, k = f'{base}-{k}', k + 1
            ids[rel] = pid
            taken_ids.add(pid)

    entries, made, kept = [], 0, 0
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
        date = date_of(meta, rel) or info['taken'] or name_date(os.path.basename(rel))
        album = rel.split('/')[0] if '/' in rel else ''
        entries.append(describe({'id': pid, 'file': rel, 'sha': fp, 'w': info['w'], 'h': info['h'],
                                 'widths': info['widths'], 'taken': info['taken'], 'date': date, 'album': album}, meta))

    have_ffmpeg = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
    typing_taken = False
    for rel in uploads:
        path, pid = os.path.join(SRC, rel), ids[rel]
        fp = fingerprint(path)
        prev = old.get(rel) or {}
        unchanged = (prev.get('sha') == fp and prev.get('id') == pid and os.path.exists(os.path.join(OUT, f'{pid}.mp4'))
                     and all(os.path.exists(os.path.join(OUT, f'{pid}-{x}.webp')) for x in prev.get('widths', [])))
        if unchanged:
            info = {k: prev.get(k) for k in ('w', 'h', 'widths', 'duration', 'taken')}
            kept += 1
        elif not have_ffmpeg:
            warn(f'{rel}: converting videos needs ffmpeg (the GitHub Action installs it); skipped here.')
            failed += 1
            continue
        else:
            try:
                tags = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=creation_time',
                                       '-of', 'default=nw=1:nk=1', path], capture_output=True, text=True).stdout
                m = re.match(r'(\d{4})-(\d{2})-(\d{2})', tags.strip())
                info = make_video(path, pid)
                info['taken'] = '-'.join(m.groups()) if m and valid_date(*m.groups()) else ''
                made += 1
                print(f'  video {rel}: {info["w"]}x{info["h"]} cover, {info["duration"]} s, {info["size"] / 1048576:.1f} MB')
            except Exception as e:
                warn(f'{rel} could not be converted as a video ({e}); skipped.')
                failed += 1
                continue
        meta = next((captions[k] for k in caption_keys(rel) if k in captions), {})
        entry = {'id': pid, 'type': 'video', 'file': rel, 'url': f'assets/gallery/{pid}.mp4', 'sha': fp,
                 'w': info['w'], 'h': info['h'], 'widths': info['widths'], 'duration': info.get('duration') or 0,
                 'taken': info.get('taken') or '',
                 'date': date_of(meta, rel) or info.get('taken') or name_date(os.path.basename(rel)),
                 'album': rel.split('/')[0] if '/' in rel else ''}
        if re.sub(r'[\s_-]+', '', os.path.splitext(os.path.basename(rel))[0].lower()) in ('typing', 'touchtyping'):
            if typing_taken:
                warn(f'{rel}: another typing video is already used for the Touch Typing block; this one is listed with the photos.')
            else:
                entry['role'] = 'typing'   # shown in the Touch Typing block, not in the grid
                typing_taken = True
        entries.append(describe(entry, meta))

    used = {k for e in entries for k in caption_keys(e['file'])}
    for key in captions:
        if key not in used:
            warn(f'gallery/captions.yml names "{captions[key]["file"]}", but no such photo is in gallery/.')

    # videos: a link in captions.yml; the cover is a photo named in "file:" or the video site's own image
    for v, (provider, vid, link) in usable:
        pid = 'video-' + hashlib.sha1(link.encode()).hexdigest()[:10]
        cover_rel = cover_of.get(str(v.get('file') or '').strip().replace('\\', '/').lower(), '')
        if v.get('file') and not cover_rel:
            warn(f'gallery/captions.yml: the cover "{v["file"]}" for {link} is not in gallery/; the video site\'s image is used.')
        cover_fp = fingerprint(os.path.join(SRC, cover_rel)) if cover_rel else 'from-' + provider
        prev = old.get('video:' + link) or {}
        unchanged = (prev.get('id') == pid and prev.get('sha') == cover_fp and bool(prev.get('widths'))
                     and all(os.path.exists(os.path.join(OUT, f'{pid}-{x}.webp')) for x in prev['widths']))
        info = {'w': 16, 'h': 9, 'widths': []}   # no cover: the tile shows a plain play button
        if unchanged:
            info = {k: prev[k] for k in ('w', 'h', 'widths')}
            kept += 1
        else:
            try:
                if cover_rel:
                    info = make_copies(os.path.join(SRC, cover_rel), pid)
                else:
                    data = video_cover(provider, vid, link)
                    if data:
                        with tempfile.TemporaryDirectory() as tmp:
                            im = Image.open(io.BytesIO(data))
                            im.load()
                            cover = os.path.join(tmp, 'cover.png')
                            without_letterbox(im.convert('RGB')).save(cover)
                            info = make_copies(cover, pid)
                made += 1
                print(f'  video {link}: cover {"from " + cover_rel if cover_rel else ("downloaded" if info["widths"] else "none")}')
            except Exception as e:
                warn(f'the cover image of {link} could not be made ({e}); its tile shows a plain play button.')
                info = {'w': 16, 'h': 9, 'widths': []}
        entries.append(describe({'id': pid, 'type': 'video', 'url': link, 'file': cover_rel, 'sha': cover_fp,
                                 'w': info['w'], 'h': info['h'], 'widths': info['widths'], 'taken': '',
                                 'date': date_of(v, link)}, v))

    # newest first; items without a date last, by name
    entries.sort(key=lambda e: (e.get('file') or e.get('url', '')).lower())
    entries.sort(key=lambda e: e['date'], reverse=True)

    # remove copies that no listed item uses any more
    wanted = {f'{e["id"]}-{x}.webp' for e in entries for x in e['widths']}
    wanted |= {f'{e["id"]}.mp4' for e in entries if e.get('type') == 'video' and e.get('url', '').startswith('assets/')}
    removed = 0
    for name in os.listdir(OUT):
        if re.fullmatch(r'[a-z0-9-]+(-\d+\.webp|\.mp4)', name) and name not in wanted:
            os.remove(os.path.join(OUT, name))
            removed += 1

    with open(LIST, 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'photos': entries}, f, ensure_ascii=False, indent=1)
        f.write('\n')
    n_vid = sum(1 for e in entries if e.get('type') == 'video')
    print(f'Gallery: {len(entries) - n_vid} photo(s) and {n_vid} video(s) listed; {made} processed, {kept} unchanged, '
          f'{failed} skipped, {removed} old file(s) removed.')


if __name__ == '__main__':
    main()
