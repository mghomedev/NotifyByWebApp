"""Embed the PNG icons produced by scripts/make_icons.ps1 into notify_icons.py.

Usage:  python scripts/embed_icons.py <dir-with-pngs>

The icons are embedded as base64 so the Vercel serverless bundle always
contains them without relying on static-file bundling.
"""
import base64
import pathlib
import sys

FILES = {
    "ICON_192": "icon-192.png",
    "ICON_512": "icon-512.png",
    "APPLE_TOUCH_ICON": "apple-touch-icon.png",
    "BADGE": "badge.png",
}

HEADER = '''"""App icons (PNG, base64-embedded — see scripts/make_icons.ps1 +
scripts/embed_icons.py to regenerate). ICON_192/512 + APPLE_TOUCH_ICON are
full-bleed OPAQUE squares (safe for the PWA "maskable" purpose and iOS, which
must not get transparency). BADGE is a MONOCHROME, TRANSPARENT silhouette used
as the Android notification small icon (Android masks it to its alpha channel,
so an opaque icon would show as a plain white square)."""
import base64

'''


def main() -> None:
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out = pathlib.Path(__file__).resolve().parent.parent / "notify_icons.py"
    parts = [HEADER]
    for name, fname in FILES.items():
        data = (src / fname).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{fname} is not a PNG"
        b64 = base64.b64encode(data).decode()
        lines = "\n".join(
            '    "%s"' % b64[i : i + 96] for i in range(0, len(b64), 96)
        )
        parts.append(f"_{name}_B64 = (\n{lines}\n)\n\n")
    parts.append(
        "\n".join(
            f"{name}: bytes = base64.b64decode(_{name}_B64)" for name in FILES
        )
        + "\n"
    )
    out.write_text("".join(parts), encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
