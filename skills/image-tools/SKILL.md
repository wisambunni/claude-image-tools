---
name: image-tools
description: How to inspect and edit images with the image-tools MCP server (view, grid_overlay, zoom, split_tiles, crop, resize, rotate/flip, adjust, convert, copy to clipboard). Use whenever the user attaches or pastes an image in chat and wants it edited or examined closely, asks to crop/resize/convert/rotate an image file, mentions HEIC/WebP/AVIF/TIFF files, or when fine detail in a screenshot, photo or scanned document is hard to read.
---

# Working with images

The `image-tools` MCP server gives you eyes and hands for image files on disk. Tools that change pixels return a preview so you can check the result instead of assuming it is right.

## Images the user shares in chat

Every tool's `path` accepts more than file paths:

| `path` | Means |
|---|---|
| `"chat"` | The latest image the user attached or pasted in this conversation |
| `"chat:2"`, `"chat:3"` | The one before that, and so on |
| `"clipboard"` | The image on the clipboard (works right after the user pastes or copies one) |
| `"screenshot"` | The newest screenshot on disk |

When the user attaches an image and says "crop this", "make this smaller", "convert this to PNG", call the tool with `path: "chat"` straight away. Don't ask them to save it somewhere first. With several attachments, call `find_images` with `where: "chat"` to see them numbered.

If `"chat"` isn't available (the error says so, e.g. in Claude Desktop chat), try `"clipboard"`, then `find_images` with `where: "recent"`, and match the thumbnails against the image in the conversation.

Results from chat and clipboard images are saved to `~/Downloads/Claude Images/` (or `IMAGE_TOOLS_OUTPUT_DIR`). Tell the user where the file is. Offer `copy_to_clipboard` so they can paste the result directly.

## Seeing an image well

You view images downscaled to roughly 1568px on the long edge. Detail smaller than that is lost, so:

1. **`image_info`** first for anything you will edit: exact size, format, mode, EXIF. Every tool uses the EXIF-rotated orientation, the same one shown in previews.
2. **`view_image`** for an overview. It also opens formats the Read tool can't (HEIC, TIFF, AVIF, BMP, ICO).
3. **`zoom_image`** on a region to read small text, UI labels, numbers on charts, or distant objects. It crops the full-resolution original, so it shows real detail rather than upscaled blur. Add `enhance: true` for faint or low-contrast text.
4. **`split_tiles`** for large dense images (long screenshots, scanned pages, maps, spreadsheets) when you need to read everything. 2x2 or 1x3 is usually enough; tiles overlap so nothing falls on a seam.
5. **`adjust_image`** (preview only by default) to brighten dark photos or autocontrast faded scans before reading them.

Don't claim to have read text you could only see blurrily. Zoom in and confirm.

## Getting coordinates right

Guessing pixel coordinates from a downscaled view is the main source of bad crops. Instead:

1. Call **`grid_overlay`**. Its labels are in original-image pixels even when the preview is scaled down.
2. Read the coordinates of the region off the grid lines. Use a finer grid (e.g. 16x16) for small targets.
3. Optionally check the region with **`zoom_image`** before writing anything.
4. Then **`crop_image`** and look at the returned preview. If it's off, adjust and crop again with `overwrite: true`.

`units: "fraction"` (0-1) is convenient for requests like "the left half" or "the top third".

## Editing

- **`crop_image`**: box is `left, top, right, bottom` (right/bottom exclusive).
- **`resize_image`**: `scale`, or `width`/`height`. With both, choose `mode`: `fit` (inside the box), `fill` (cover then center-crop, good for thumbnails), `pad` (letterbox to an exact size), `stretch`. Use `resample: "nearest"` for pixel art and `allow_upscale: false` to only shrink.
- **`rotate_flip_image`**: degrees are counter-clockwise; use `-90` to turn clockwise.
- **`convert_image`**: png, jpg, webp, gif, bmp, tiff, ico, avif, heic, pdf. Transparency is flattened onto `background` for jpg/bmp/pdf. Use `all_frames: true` to keep animation.

## Safety defaults

- Originals are never modified. Outputs go next to the source as `<name>_crop.png`, `<name>_800x600.jpg` and so on, with a numeric suffix rather than overwriting. Only pass `overwrite: true` when the user asked to replace a file, or when redoing your own output.
- Pass absolute paths. Relative paths resolve against the MCP server's working directory, which may not be the project.
- When the user asks for many files (e.g. "convert this folder to WebP"), list the files first and call the tool once per file. Report what was written.
- `image_info` reports when GPS location is embedded. Mention it if the user is preparing images to share.
