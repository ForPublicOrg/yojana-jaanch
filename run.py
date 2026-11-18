"""One entry point: collect, check, refresh (collect then check), queue, serve, publish, drive-copy and drive-links.

    python run.py refresh --scheme jsjb --state Punjab --district Sangrur --block Sangrur
    python run.py collect --scheme jsjb --state Punjab --village Duggan
    python run.py queue --plan plans/jsjb-india.json [--dry-run]   # areas in order, for days
    python run.py queue --plan plans/<id>-<scope>.json             # another scheme, side by side
    python run.py collect --scheme <id> --list-only ...   then   python run.py collect --scheme <id> --photos
    python run.py check [--vision] [--data-dir data/_trial/<id>]
    python run.py serve [--port 8000]
    python run.py publish              # blurred public photo copies in publish/ (needs .venv-publish;
                                       # uses the graphics chip through .venv-gpu when that is set up)
    python run.py phash [--data-dir D] # picture fingerprints for the near-duplicate match (needs .venv-publish)
    python run.py collect --scheme jsjb --refetch-unhashed   # download again photos deleted before phash saw them
    python run.py drive-copy --local-folder <Drive sync folder>   # approved copies into subfolders 0 to f
    python run.py drive-links --folder-id <id> --local-folder <Drive sync folder>   # or set GOOGLE_API_KEY

Unknown options are passed through to the collector or checker.
No AI is used unless --vision is given.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CHECK_FLAGS = {"--vision": 0, "--vision-limit": 1}


def split_check_flags(rest):
    collect, check = [], []
    i = 0
    while i < len(rest):
        arg = rest[i]
        name = arg.split("=", 1)[0]
        if name in CHECK_FLAGS:
            take = 1 if "=" in arg else 1 + CHECK_FLAGS[name]
            check += rest[i:i + take]
            i += take
        else:
            collect.append(arg)
            i += 1
    return collect, check


def run(module, args):
    cmd = [sys.executable, "-m", module] + args
    print("> " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


VENV = os.path.join(ROOT, ".venv-publish")
VENV_GPU = os.path.join(ROOT, ".venv-gpu")
SETUP_HELP = """The publish and phash steps need OpenCV, numpy and Pillow in a separate virtual environment.
Set it up once with these two commands, then run the step again:

    python -m venv .venv-publish
    {pip} install -r requirements-publish.txt
"""
GPU_CHECK = ("import cv2, numpy, onnxruntime as o; "
             "assert 'DmlExecutionProvider' in o.get_available_providers()")


def venv_python(venv=None):
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        path = os.path.join(venv or VENV, *rel)
        if os.path.exists(path):
            return path
    return None


def works(py, code):
    return bool(py) and subprocess.call([py, "-c", code], cwd=ROOT,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def gpu_python():
    """Python in .venv-gpu if its onnxruntime can use DirectML; None when PUBLISH_ONNX_PROVIDER=cpu."""
    if os.environ.get("PUBLISH_ONNX_PROVIDER", "").strip().lower() == "cpu":
        return None
    py = venv_python(VENV_GPU)
    return py if works(py, GPU_CHECK) else None


def publish(rest, step="publish", needs="import cv2, numpy"):
    """publish and phash run in .venv-publish (OpenCV, numpy, Pillow). publish runs the blur models
    on the graphics chip from .venv-gpu (requirements-publish-gpu.txt) when it is set up."""
    gpu = gpu_python() if step == "publish" else None
    if gpu:
        cmd = [gpu, "-m", "publisher", step] + rest
        print("> PUBLISH_ONNX_PROVIDER=dml " + " ".join(cmd), flush=True)
        return subprocess.call(cmd, cwd=ROOT, env=dict(os.environ, PUBLISH_ONNX_PROVIDER="dml"))
    py = venv_python()
    pip = os.path.join(".venv-publish", "Scripts", "pip") if os.name == "nt" else ".venv-publish/bin/pip"
    if not works(py, needs):
        print(SETUP_HELP.format(pip=pip), flush=True)
        return 3
    cmd = [py, "-m", "publisher", step] + rest
    print("> " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


# Commands whose -h/--help goes to the step itself (it prints its options and exits). Not refresh:
# its collector would print help and then a full check would run.
STEP_HELP = ("collect", "check", "queue", "phash")


def main():
    argv = sys.argv[1:]
    step_help = bool(argv) and argv[0] in STEP_HELP and any(a in ("-h", "--help") for a in argv[1:])
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                add_help=not step_help)
    p.add_argument("command", choices=["refresh", "collect", "queue", "check", "serve", "publish", "phash",
                                       "drive-copy", "drive-links"])
    p.add_argument("--scheme", help="scheme id (collect default: jsjb; check default: all)")
    p.add_argument("--port", type=int, default=8000)
    opts, rest = p.parse_known_args()

    if opts.command == "serve":
        print(f"Serving site/ at http://localhost:{opts.port}/", flush=True)
        return subprocess.call([sys.executable, "-m", "http.server", str(opts.port),
                                "--directory", os.path.join(ROOT, "site")])

    if opts.command == "publish":
        return publish(rest)

    if opts.command == "phash":
        return publish(rest, "phash", "import PIL")

    if opts.command == "queue":
        return run("collector.queue_runner", rest)

    if opts.command in ("drive-copy", "drive-links"):
        return run("publisher", [opts.command] + rest)

    if opts.command == "check":
        return run("checker", rest + (["--scheme", opts.scheme] if opts.scheme else []))

    collect_args, check_args = split_check_flags(rest)
    # The checker reads the folder the collector wrote to.
    for i, arg in enumerate(collect_args):
        if arg == "--data-dir" and i + 1 < len(collect_args):
            check_args += collect_args[i:i + 2]
        elif arg.startswith("--data-dir="):
            check_args.append(arg)
    code = run("collector", [opts.scheme or "jsjb"] + collect_args)
    if opts.command == "collect":
        return code
    if code not in (0, 130):
        print(f"Collector stopped with exit code {code}; checking what was collected so far.", flush=True)
    check_code = run("checker", check_args)
    return code or check_code


if __name__ == "__main__":
    sys.exit(main())
