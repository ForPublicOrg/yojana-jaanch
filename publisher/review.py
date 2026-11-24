"""publish/review.html: a local page to look over every published copy before uploading."""
import html

from . import common

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review blurred photos</title>
<style>
body {{ font: 15px/1.45 system-ui, sans-serif; margin: 0 auto; padding: 16px; max-width: 1400px; color: #1a1a1a; background: #fafafa; }}
h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
h2 {{ font-size: 1.1rem; margin: 28px 0 8px; }}
p {{ margin: 4px 0 8px; max-width: 70ch; }}
code {{ font-size: 0.85em; word-break: break-all; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }}
figure {{ margin: 0; background: #fff; border: 1px solid #d0d0d0; border-radius: 6px; padding: 8px; }}
.frame {{ position: relative; line-height: 0; }}
.frame img {{ width: 100%; height: auto; cursor: crosshair; }}
.box {{ position: absolute; border: 2px solid #c00; box-sizing: border-box; pointer-events: none; border-radius: 50%; }}
.box span {{ position: absolute; left: 0; top: -1.5em; font: 11px/1.3 system-ui, sans-serif; background: #fff; color: #1a1a1a;
  padding: 0 3px; white-space: nowrap; border-radius: 2px; }}
.box.head {{ border-color: #b35900; border-style: dashed; }}
.box.extra {{ border-color: #06c; border-radius: 0; }}
.key span {{ display: inline-block; width: 14px; height: 10px; border: 2px solid #c00; border-radius: 50%; vertical-align: middle; }}
.key .head {{ border-color: #b35900; border-style: dashed; }}
.key .extra {{ border-color: #06c; border-radius: 0; }}
body.nobox .box {{ display: none; }}
figcaption {{ margin-top: 6px; }}
.zero {{ color: #a00; font-weight: 600; }}
.warn {{ background: #fde8e4; border: 1px solid #c00; padding: 8px 12px; border-radius: 6px; max-width: 70ch; }}
.ok {{ color: #1a6b2a; font-weight: 600; }}
.gone {{ color: #6b4a1a; }}
textarea {{ width: 100%; max-width: 900px; height: 7em; font: 12px monospace; }}
figure label {{ display: flex; gap: 8px; align-items: center; min-height: 44px; }}
.coord {{ color: #333; min-height: 1.4em; }}
label {{ display: inline-block; margin: 6px 0; }}
</style>
</head>
<body>
<h1>Review blurred photos</h1>
{warning}<p>{total} blurred copies, {approved} approved, {waiting} waiting for a check. Only approved copies go to
publish/upload/ and into the photo links.</p>
<p class="warn">The engines can still miss a face, most often a very small, far away or half hidden one, also in photos
where they found other faces. An outline only shows where the copy was blurred, not that the photo is safe. Look at every
person in every photo.</p>
<p class="key">Outlines: <span></span> face found by the face engine (YOLOv12m-face),
<span class="head"></span> head placed from body keypoints (YOLO11x-pose), <span class="extra"></span> your box from
extra_blur.json. Each outline is labelled with the engine that found it.</p>
{auto}
<p>If a face is still recognisable, either add its sha256 to publish/hide.txt, or click its top-left and bottom-right corners
to get a box for publish/extra_blur.json, then run <code>python run.py publish</code> again.
If no face can be recognised, tick "No recognisable face" and paste the lines below into publish/approved.txt.</p>
<label><input type="checkbox" id="toggle" checked> Show outlines</label>
<p><label for="lines">Lines for publish/approved.txt</label><br><textarea id="lines" readonly></textarea></p>
{sections}
<script>
document.getElementById('toggle').addEventListener('change', function (e) {{
  document.body.classList.toggle('nobox', !e.target.checked);
}});
function lines() {{
  document.getElementById('lines').value = Array.prototype.map.call(
    document.querySelectorAll('input.approve:checked'), function (c) {{ return c.value; }}).join('\\n');
}}
document.querySelectorAll('input.approve').forEach(function (c) {{ c.addEventListener('change', lines); }});
document.querySelectorAll('figure').forEach(function (fig) {{
  var img = fig.querySelector('img'), out = fig.querySelector('.coord'), first = null;
  img.addEventListener('click', function (e) {{
    var r = img.getBoundingClientRect();
    var x = Math.round((e.clientX - r.left) * img.naturalWidth / r.width);
    var y = Math.round((e.clientY - r.top) * img.naturalHeight / r.height);
    if (!first) {{ first = [x, y]; out.textContent = 'Top-left ' + x + ', ' + y + '. Now click the bottom-right corner.'; return; }}
    var box = [Math.min(first[0], x), Math.min(first[1], y), Math.abs(x - first[0]), Math.abs(y - first[1])];
    out.textContent = '"' + fig.dataset.sha + '": [' + JSON.stringify(box) + ']';
    first = null;
  }});
}});
</script>
</body>
</html>
"""


LABELS = {"yoloface-m": "face", "yolopose": "head (pose)"}


def figure(sha, e, ok=False, auto=False, gone=False):
    w, h = e.get("width") or 1, e.get("height") or 1
    found = e.get("boxes", [])
    engines = e.get("box_engines") or [str(e.get("detector", "unknown"))] * len(found)
    kinds = e.get("box_kinds") or ["face"] * len(found)
    marks = [(f" {k}" if k == "head" else "", LABELS.get(g, g), b) for b, g, k in zip(found, engines, kinds)]
    marks += [(" extra", "extra_blur.json", b) for b in e.get("extra_boxes", [])]
    boxes = "".join(
        f'<div class="box{cls}" style="left:{100 * b[0] / w:.2f}%;top:{100 * b[1] / h:.2f}%;'
        f'width:{100 * b[2] / w:.2f}%;height:{100 * b[3] / h:.2f}%"><span>{html.escape(label)}</span></div>'
        for cls, label, b in marks)
    n = e.get("faces", 0)
    if n == 0 and e.get("people_signal", 1) == 0:
        count = '<span class="zero">No face or person found</span>'
    elif n == 0:
        count = '<span class="zero">No face or head found, but an engine saw a possible person</span>'
    else:
        count = f"{n} face{'s' if n != 1 else ''} or head{'s' if n != 1 else ''} blurred"
    extra = f", {len(e['extra_boxes'])} extra box{'es' if len(e['extra_boxes']) != 1 else ''}" if e.get("extra_boxes") else ""
    if ok:
        approve = '<div class="ok">Approved automatically: no person found</div>' if auto else '<div class="ok">Approved</div>'
    elif gone:
        approve = '<div class="gone">This copy is no longer in publish/photos, so it cannot be checked here</div>'
    elif e.get("copy_sha256"):
        line = html.escape(common.approval_line(sha, e["copy_sha256"]))
        approve = f'<label><input type="checkbox" class="approve" value="{line}"> No recognisable face</label>'
    else:
        approve = ""
    return (f'<figure data-sha="{html.escape(sha)}"><div class="frame">'
            f'<img src="photos/{html.escape(sha)}.jpg" loading="lazy" alt="Blurred copy {html.escape(sha[:12])}">'
            f'{boxes}</div><figcaption>{count}{extra}'
            f'<br><code>{html.escape(sha)}</code>{approve}'
            f'<div class="coord"></div></figcaption></figure>')


def write(paths, manifest, approved=None, settings=None):
    approved = approved or {}
    settings = settings or common.DEFAULT_SETTINGS
    auto_lines = set()
    if paths.approved.exists():
        auto_lines = {line.split()[0].lower() for line in paths.approved.read_text(encoding="utf-8").splitlines()
                      if line.endswith((common.AUTO_NOTE, common.ALL_NOTE))}
    items = sorted(manifest.items(), key=lambda kv: (kv[1].get("faces", 0) > 0, kv[0]))
    done = {sha for sha, e in items if common.is_approved(approved, sha, e)}
    waiting = [kv for kv in items if kv[0] not in done]
    checked = [kv for kv in items if kv[0] in done]
    sections = ""
    if waiting:
        sections += (f"<h2>Waiting for a check ({len(waiting)})</h2><div class=\"grid\">"
                     + "".join(figure(*kv, gone=not (paths.out_photos / f"{kv[0]}.jpg").exists()) for kv in waiting)
                     + "</div>")
    if checked:
        sections += (f"<h2>Approved ({len(checked)})</h2><div class=\"grid\">"
                     + "".join(figure(*kv, ok=True, auto=kv[0] in auto_lines) for kv in checked) + "</div>")
    auto = ""
    if settings.get("auto_approve_all"):
        auto = ('<p class="warn">auto_approve_all is on in publish/settings.json: every blurred copy is approved '
                f'in bulk without a check ({len(auto_lines)} so far). The engines can still miss a face; a photo '
                'found showing one goes in hide.txt and is deleted from the Drive folder.</p>')
    elif settings.get("auto_approve_no_person"):
        auto = ('<p class="warn">auto_approve_no_person is on in publish/settings.json: copies in which no engine '
                f'found any face or person ({len(auto_lines)}) were approved without a check.</p>')
    paths.review.write_text(PAGE.format(total=len(items), approved=len(checked), waiting=len(waiting),
                                        warning="", auto=auto, sections=sections), encoding="utf-8")
