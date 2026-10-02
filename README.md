# claude-image-tools

Gives Claude better eyes and hands for image files: zoom into regions at full resolution, overlay pixel grids for precise coordinates, split large images into tiles, and crop, resize, rotate, adjust and convert between formats.

It ships as both:

- a **Claude Code plugin** (MCP server + a skill that teaches Claude when to use each tool), and
- a **Claude Desktop extension** (`.mcpb`) built from the same server code.

## Why

Claude sees images downscaled to about 1568px on the long edge, so small text in screenshots, scanned documents and photos gets lost, and guessed crop coordinates are often off. These tools let Claude look closer before it answers and check every edit it makes.

## Tools

| Tool | What it does | Writes files? |
|---|---|---|
| `image_info` | Size, format, mode, file size, frames, ICC, key EXIF (flags embedded GPS) | no |
| `view_image` | Shows any image, including HEIC, TIFF, AVIF, BMP and ICO | no |
| `grid_overlay` | Labeled pixel-coordinate grid, in original-image pixels | no |
| `zoom_image` | Crops a region from the full-resolution original and enlarges it, with optional contrast/sharpen | no |
| `split_tiles` | Up to 16 overlapping tiles at full detail, for big dense images | optional |
| `adjust_image` | Brightness, contrast, sharpness, saturation, autocontrast, grayscale | optional |
| `crop_image` | Pixel or 0–1 fractional box | yes |
| `resize_image` | Scale, or width/height with `fit` / `fill` / `pad` / `stretch`; selectable resampling | yes |
| `rotate_flip_image` | Any angle, lossless 90° steps, horizontal/vertical mirror | yes |
| `convert_image` | PNG, JPEG, WebP, HEIC, AVIF, GIF, TIFF, BMP, ICO, PDF; animated GIF/WebP | yes |
| `find_images` | Numbered thumbnails of chat attachments and recently changed images on disk | no |
| `copy_to_clipboard` | Puts an image (e.g. an edited result) on the clipboard to paste anywhere | no |

Tools that change pixels return a preview, so Claude checks its own result.

## Images you share in chat

You don't need to save an image anywhere first. Attach or paste it and say "crop this to the right half" or "convert this to PNG". Every tool's `path` also accepts:

| `path` | Means |
|---|---|
| `chat` | The latest image you attached in this conversation (`chat:2` is the one before it) |
| `clipboard` | The image on your clipboard, e.g. one you just pasted |
| `screenshot` | Your newest screenshot |

Edited versions of chat and clipboard images are saved to `~/Downloads/Claude Images/` (change it with `IMAGE_TOOLS_OUTPUT_DIR`). Claude can also put the result on your clipboard.

How it works, and why it differs by app: Claude sees an attached image, but it can't pass the image's bytes into a tool call, and MCP has no way to forward attachments to tools. So the server fetches the image itself:

- **Claude Code** (terminal, the desktop app's Code tab, or an uploaded plugin): `chat` reads the image from the session transcript Claude Code keeps on disk (`~/.claude/projects/…`), using the `CLAUDE_CODE_SESSION_ID` the server receives. This works for every image you attach.
- **Claude Desktop chat** (via the `.mcpb` extension): attachments live only on Anthropic's servers, so `chat` isn't available. If you pasted the image, `clipboard` still has it. Otherwise Claude uses `find_images` to show recent images from your Desktop, Downloads, Pictures, Documents and screenshot folders, and matches them against what you attached.

Safety defaults: originals are never modified. Outputs go next to the source (`photo_crop.png`, `photo_800x600.jpg`, …) and get a numeric suffix instead of overwriting, unless `overwrite: true` is passed. EXIF orientation is applied on load, so coordinates match what Claude sees.

## Requirements

[uv](https://docs.astral.sh/uv/) installs Python and the dependencies (Pillow, pillow-heif, mcp) automatically on first run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The first launch takes a little longer while dependencies download.

## Install in Claude Code

From GitHub (after pushing this repo):

```text
/plugin marketplace add <github-user>/claude-image-tools
/plugin install image-tools@claude-image-tools
```

Or from a local clone:

```text
/plugin marketplace add /path/to/claude-image-tools
/plugin install image-tools@claude-image-tools
```

To try it without installing:

```bash
claude --plugin-dir /path/to/claude-image-tools
```

## Install in Claude Desktop

### As a plugin (recommended)

The same plugin uploads directly to Claude Desktop. Build the uploadable file:

```bash
./scripts/build-plugin.sh
```

Then upload `dist/image-tools.plugin` in Claude Desktop's plugin settings. You get the MCP tools and the skill, just as in Claude Code.

### As a Desktop extension (alternative)

For setups that use Desktop extensions instead of plugins, build an `.mcpb` (needs Node for `npx`):

```bash
./scripts/build-mcpb.sh
```

Then double-click `dist/image-tools.mcpb`, or drag it into **Settings → Extensions**. Extensions don't carry skills, so the server also sends its usage guidance as MCP `instructions`.

Tools need a **file path**. Give Claude the path of the image on disk (for example `~/Desktop/screenshot.png`), not just a chat attachment.

## Use with other MCP clients

Any MCP client can run the server directly:

```json
{
  "mcpServers": {
    "image-tools": {
      "command": "uv",
      "args": ["run", "--quiet", "--script", "/path/to/claude-image-tools/server/image_tools.py"]
    }
  }
}
```

## Development

```text
.claude-plugin/plugin.json       Claude Code plugin manifest
.claude-plugin/marketplace.json  Lets this repo be added as a plugin marketplace
.mcp.json                        Starts the MCP server for Claude Code
skills/image-tools/SKILL.md      Guidance Claude Code loads on image tasks
server/image_tools.py            The MCP server (single file, PEP 723 inline deps)
desktop/                         Claude Desktop extension manifest, pyproject and icon
scripts/build-plugin.sh          Builds dist/image-tools.plugin (upload to Claude Desktop)
scripts/build-mcpb.sh            Builds dist/image-tools.mcpb (Desktop extension)
tests/smoke_test.py              End-to-end test of every tool over MCP stdio
```

Run the tests:

```bash
uv run --script tests/smoke_test.py
```

Check the plugin manifests:

```bash
claude plugin validate .
```

When releasing, bump the version in `.claude-plugin/plugin.json`, `desktop/manifest.json` and `desktop/pyproject.toml`.

## Troubleshooting

- **Server fails to start / `uv: command not found`**: apps launched from the Dock may not see `~/.local/bin`. Install uv with Homebrew (`brew install uv`), or link it: `ln -s ~/.local/bin/uv /usr/local/bin/uv`.
- **HEIC errors**: pillow-heif ships wheels for Python ≤3.13. The server pins `<3.14` so uv picks a compatible interpreter.
