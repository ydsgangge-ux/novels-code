# Bundled desktop-pet assets

Default clips so the pet shows something on a fresh clone. One folder per
action: `idle/`, `thinking/`, `working/`, `success/`, `error/`, `sleep/`.

Currently shipped: `idle`, `working`, `error` (VP9 + alpha WebM, ~200 KB each).
Missing actions fall back to `idle`.

These are only fallbacks. Files in `.gangge_data/pet/<action>/` win over the
bundled ones and are picked first, so you can drop your own clips there without
touching the repo (that folder is git-ignored).

To convert a solid-background clip, right-click the pet window →
"自动抠像": it writes a transparent APNG into `.gangge_data/pet/<action>/` and
renames the source video to `*.mp4.source`.

Encoding tip for extra clips: VP9 with alpha keeps edges clean and stays small.

```
ffmpeg -i input.apng -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 0 -crf 28 -an out.webm
```
