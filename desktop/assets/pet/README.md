# Bundled desktop-pet assets

Default clips so the pet shows something on a fresh clone. One folder per
action: `idle/`, `thinking/`, `working/`, `success/`, `error/`, `sleep/`.

Currently shipped: `idle`, `working`, `error` (VP9 + alpha WebM, ~200 KB each).
Missing actions fall back to `idle`.

**This folder is never played directly.** On first launch the files are copied
into the user's asset folder `.gangge_data/pet/<action>/`, so they show up where
users expect them and can be deleted there with a right-click → "打开素材文件夹".
Deleting a copied file keeps it deleted; right-click → "恢复内置表情" copies the
missing ones back. Only actions that are still empty get seeded, so your own
clips are never overwritten.

To convert a solid-background clip, right-click the pet window →
"自动抠像": it writes a transparent APNG into `.gangge_data/pet/<action>/` and
renames the source video to `*.mp4.source`.

Encoding tip for extra clips: VP9 with alpha keeps edges clean and stays small.

```
ffmpeg -i input.apng -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 0 -crf 28 -an out.webm
```
