# Photos & Videos

Photos in this folder, and videos linked in `captions.yml`, appear under **Beyond Research → Photos & Videos** on <https://srujan721.github.io/#gallery>.

## Add photos

1. In this `gallery` folder on GitHub, choose **Add file → Upload files**.
2. Drop the photos (JPEG, PNG, WebP or iPhone HEIC, any size up to GitHub's 25 MB per file) and click **Commit changes**.
3. Wait about two to five minutes. A GitHub Action makes web-size copies with all metadata removed, lists them in `assets/gallery/gallery.json`, and the site updates. Progress and any problems are shown under the repository's **Actions** tab.

To make an album, upload the photos into a subfolder, for example `gallery/Conferences/`, or set `album:` in `captions.yml`. Album buttons appear once photos are spread over two or more albums.

Photos are shown newest first, using the camera's date. A date written in the file name (`IMG_20251203_142233.jpg`, `WhatsApp Image 2025-12-03 at ...`) is used when the camera date is missing, and `date:` in `captions.yml` overrides both.

## Add a video

**Upload the file here** (MP4, or MOV from a phone, WebM, ...). The Action converts it to a web video that plays in every browser (H.264, at most 1280 px, 30 frames/s; a 50 MB phone clip usually becomes 5 to 10 MB) and takes a cover picture from it.

- Name it **`typing.mp4`** (or `typing.mov`, any video format) to show it in the **Touch Typing** block. Any other name puts it under Photos & Videos.
- GitHub's website accepts at most **25 MB per file**. For a larger video either make it smaller first (for example HandBrake's "Fast 720p30" preset, or your phone's export at 720p), or upload it with **GitHub Desktop**, which accepts files up to 100 MB.
- Captions, dates and albums work as for photos (`file: typing.mp4` in `captions.yml`). The recording date is read from the phone's video when no date is given.

**Or link a video** that is on YouTube ("Unlisted" keeps it out of YouTube search), Vimeo, or Google Drive (shared with "Anyone with the link"), by adding an entry to `captions.yml`:

```yaml
- video: https://youtu.be/VIDEO_ID
  caption: Touch typing at 110 WPM
  date: 2026-11
  album: Typing
```

The Action downloads a linked video's cover image once. To use your own cover, upload a photo here and add `file: thatphoto.jpg` to the video's entry. Videos play on the site only when a visitor presses play.

For the Touch Typing block, a YouTube, Vimeo or Drive link can also be pasted into `index.html` (search for `data-video`); it takes precedence over an uploaded `typing` file.

## Captions, places and dates

Open `captions.yml` in this folder, click the pencil icon to edit, and add one entry per photo:

```yaml
- file: IMG_20231012_101500.jpg
  caption: "Rock Bowl semi-final, 15th ISRM Congress"
  place: Salzburg, Austria
  date: 2023-10-12
  album: Conferences
```

All fields except `file:` (or `video:`) are optional. If the file has a mistake, the Action stops without changing the site and the **Actions** tab shows the line to fix.

## Remove or replace a photo or video

Delete the file from this folder; its web copies are removed automatically. To replace one, upload the new file under the same name.

## Location data

The website shows only the copies made by the Action, which carry no location, camera or time-stamp data. The photos uploaded to this folder, however, stay in the public repository, including any GPS location the phone recorded. For a photo whose location should stay private, remove the location before uploading (most phones offer this in the photo's details or share options) or switch off location tagging in the camera settings.

## Notes

- `assets/gallery/` is written by the Action; do not edit it by hand.
- To run the same step on a computer instead: `pip install pillow pyyaml pillow-heif`, then `python tools/build_gallery.py` from the site folder.
