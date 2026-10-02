# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["mcp>=1.10,<2", "pillow>=11.0"]
# ///
"""End-to-end smoke test: launches the server over stdio and exercises every tool."""

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image, ImageDraw

SERVER = Path(__file__).resolve().parent.parent / "server" / "image_tools.py"


def make_fixtures(d: Path) -> dict[str, Path]:
    img = Image.new("RGBA", (2400, 1600), (240, 240, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((1000, 600, 1400, 1000), fill=(200, 30, 30, 255))
    draw.text((1050, 780), "tiny text", fill="white")
    draw.ellipse((100, 100, 300, 300), fill=(0, 0, 0, 0))
    png = d / "sample.png"
    img.save(png)
    gif = d / "anim.gif"
    frames = [Image.new("RGB", (64, 64), c) for c in ("red", "green", "blue")]
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return {"png": png, "gif": gif}


async def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = make_fixtures(d)
        # Fake a Claude Code session whose transcript holds two user-attached images.
        session_id = "11111111-2222-3333-4444-555555555555"
        tdir = d / "claude" / "projects" / "-fake-project"
        tdir.mkdir(parents=True)
        lines = []
        # A large original on disk in the project, attached as a 2000px copy (as Claude Code stores it).
        project = d / "project"
        project.mkdir()
        original = Image.new("RGB", (4000, 2000), "white")
        od = ImageDraw.Draw(original)
        for i in range(0, 4000, 250):
            od.rectangle((i, (i // 3) % 1600, i + 120, (i // 3) % 1600 + 300), fill=(i % 255, 80, 160))
        original.save(project / "plan.png")
        attachments = [Image.new("RGB", (640, 480), "blue"), original.resize((2000, 1000), Image.Resampling.LANCZOS),
                       Image.new("RGB", (640, 480), "green")]
        for att in attachments:
            buf = io.BytesIO()
            att.save(buf, format="PNG")
            block = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(buf.getvalue()).decode()}}
            lines.append({"type": "user", "message": {"role": "user", "content": [block, {"type": "text", "text": "hi"}]}})
        # An image returned by a tool must NOT count as a chat attachment.
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": [lines[0]["message"]["content"][0]]}]}})
        (tdir / f"{session_id}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        out_dir = d / "outputs"
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(d / "claude"), "CLAUDE_CODE_SESSION_ID": session_id,
               "CLAUDE_PLUGIN_DATA": str(d / "plugin-data"), "IMAGE_TOOLS_OUTPUT_DIR": str(out_dir), "CLAUDE_PROJECT_DIR": str(project)}
        params = StdioServerParameters(command="uv", args=["run", "--quiet", "--script", str(SERVER)], env=env)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            print("tools:", sorted(tools))

            async def call(name, **args):
                nonlocal failures
                res = await s.call_tool(name, args)
                kinds = [c.type for c in res.content]
                text = " | ".join(c.text for c in res.content if c.type == "text")
                status = "FAIL" if res.isError else "ok"
                if res.isError:
                    failures += 1
                print(f"[{status}] {name}: {kinds} {text[:160]}")
                return res

            png = str(f["png"])
            await call("image_info", path=png)
            await call("view_image", path=png)
            await call("grid_overlay", path=png, rows=4, cols=6)
            await call("zoom_image", path=png, left=1000, top=600, right=1400, bottom=1000, enhance=True)
            await call("crop_image", path=png, left=0.25, top=0.25, right=0.75, bottom=0.75, units="fraction")
            await call("resize_image", path=png, width=300, height=300, mode="fill")
            await call("resize_image", path=png, width=500, height=500, mode="pad", pad_color="black")
            await call("resize_image", path=png, scale=0.1)
            for fmt in ("jpg", "webp", "avif", "heic", "ico", "tiff", "bmp", "pdf", "gif"):
                await call("convert_image", path=png, format=fmt)
            await call("convert_image", path=str(f["gif"]), format="webp", all_frames=True)
            await call("rotate_flip_image", path=png, rotate=-90, flip="horizontal", show_result=False)
            await call("rotate_flip_image", path=png, rotate=15, show_result=False)
            await call("adjust_image", path=png, contrast=1.5, grayscale=True)
            await call("split_tiles", path=png, rows=2, cols=2, save_dir=str(d / "tiles"))
            await call("view_image", path=str(d / "sample.heic"))

            # chat attachments: chat = latest (green), chat:2 = plan (has an original on disk), chat:3 = blue
            res = await call("image_info", path="chat")
            await call("crop_image", path="chat", left=0, top=0, right=100, bottom=100)
            await call("convert_image", path="chat:3", format="webp")
            await call("find_images", where="chat")
            await call("find_images", where="recent", days=2)
            outs = sorted(p.name for p in out_dir.iterdir())
            print("chat outputs:", outs)
            colors = {n: Image.open(out_dir / n).convert("RGB").getpixel((5, 5)) for n in outs}
            if colors.get("chat-image-3_crop.png") != (0, 128, 0) or colors.get("chat-image-1.webp", (0, 0, 0))[2] < 200:
                failures += 1
                print("[FAIL] chat images resolved to the wrong attachment:", colors)
            res = await call("image_info", path="chat:2")
            if "plan.png" not in res.content[0].text or "4000x2000" not in res.content[0].text:
                failures += 1
                print("[FAIL] chat:2 should resolve to the full-resolution original plan.png")

            # detail regions, then zoom/crop by region number
            res = await call("find_detail_regions", path=png)
            await call("zoom_image", path=png, region=1)
            await call("crop_image", path=png, region=1, output_path=str(d / "region1.png"))
            res = await s.call_tool("zoom_image", {"path": str(f["gif"]), "region": 1})
            print(f"[{'ok' if res.isError else 'FAIL'}] region without find_detail_regions rejected")
            failures += 0 if res.isError else 1

            res = await s.call_tool("crop_image", {"path": "chat:4", "left": 0, "top": 0, "right": 5, "bottom": 5})
            print(f"[{'ok' if res.isError else 'FAIL'}] chat:4 rejected: {res.content[0].text[:110]}")
            failures += 0 if res.isError else 1

            if os.environ.get("RUN_CLIPBOARD_TESTS"):
                await call("copy_to_clipboard", path=png)
                res = await call("image_info", path="clipboard")
                if "2400x1600" not in res.content[0].text:
                    failures += 1
                    print("[FAIL] clipboard round-trip")

            # error handling: must be reported as tool errors, not crash the server
            for name, args in (
                ("crop_image", {"path": png, "left": 10, "top": 10, "right": 5, "bottom": 5}),
                ("image_info", {"path": str(d / "missing.png")}),
            ):
                res = await s.call_tool(name, args)
                print(f"[{'ok' if res.isError else 'FAIL'}] expected error from {name}: {res.content[0].text[:100]}")
                failures += 0 if res.isError else 1

            assert Image.open(png).size == (2400, 1600), "original modified"
            print("files:", sorted(p.name for p in d.iterdir()))
    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
