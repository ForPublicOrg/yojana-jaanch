"""Build the two Punjab map images used by the explainer video.

Reads the site's own district shapes and area totals, so the map in the video
is the map the site draws. Run from the repository root:

    python docs/explainer/tools/build_map.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1] / "assets"
STATE = "Punjab"

# Shading steps from site/DESIGN.md: 1 to 4, 5 to 19, 20 to 99, 100 or more.
STEPS = [(100, "#55100a"), (20, "#8a2014"), (5, "#b8382a"), (1, "#d8644d")]


def shade(cases):
    for floor, colour in STEPS:
        if cases >= floor:
            return colour
    return None


def main():
    shapes = json.loads((ROOT / "site/vendor/geo/districts/punjab.json").read_text(encoding="utf-8"))
    areas = json.loads((ROOT / "site/data/areas.json").read_text(encoding="utf-8"))
    counts = {name: d["cases"] for name, d in areas["states"][STATE]["districts"].items()}

    x0 = min(s["bbox"][0] for s in shapes.values())
    y0 = min(s["bbox"][1] for s in shapes.values())
    x1 = max(s["bbox"][2] for s in shapes.values())
    y1 = max(s["bbox"][3] for s in shapes.values())
    pad = 0.02
    box = f"{x0 - pad:.3f} {y0 - pad:.3f} {x1 - x0 + 2 * pad:.3f} {y1 - y0 + 2 * pad:.3f}"
    head = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">'

    base, flagged = [head], [head]
    for name, s in shapes.items():
        base.append(
            f'<path d="{s["d"]}" fill="#f1f0eb" stroke="#8a877f" stroke-width="2" '
            'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
        )
        colour = shade(counts.get(name, 0))
        if colour:
            flagged.append(
                f'<path d="{s["d"]}" fill="{colour}" stroke="#ffffff" stroke-width="2" '
                'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
            )
    base.append("</svg>")
    flagged.append("</svg>")
    OUT.mkdir(exist_ok=True)
    (OUT / "punjab-base.svg").write_text("\n".join(base), encoding="utf-8")
    (OUT / "punjab-flagged.svg").write_text("\n".join(flagged), encoding="utf-8")

    width = x1 - x0 + 2 * pad
    height = y1 - y0 + 2 * pad
    print(f"aspect {width / height:.4f}")
    for name, n in counts.items():
        cx, cy = shapes[name]["c"]
        print(name, n, f"{(cx - x0 + pad) / width:.4f}", f"{(cy - y0 + pad) / height:.4f}")


if __name__ == "__main__":
    main()
