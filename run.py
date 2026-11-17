"""One entry point: collect, check, refresh (collect then check), queue, serve, publish and drive-links.

    python run.py refresh --scheme jsjb --state Punjab --district Sangrur --block Sangrur
    python run.py collect --scheme jsjb --state Punjab --village Duggan
    python run.py queue --plan plans/jsjb-india.json [--dry-run]   # areas in order, for days
    python run.py check [--vision]
    python run.py serve [--port 8000]
    python run.py publish              # blurred public photo copies in publish/ (needs .venv-publish)
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
SETUP_HELP = """The publish step needs OpenCV and numpy in a separate virtual environment.
Set it up once with these two commands, then run python run.py publish again:

    python -m venv .venv-publish
    {pip} install -r requirements-publish.txt
"""


def venv_python():
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        path = os.path.join(VENV, *rel)
        if os.path.exists(path):
            return path
    return None


def publish(rest):
    py = venv_python()
    pip = os.path.join(".venv-publish", "Scripts", "pip") if os.name == "nt" else ".venv-publish/bin/pip"
    if not py or subprocess.call([py, "-c", "import cv2, numpy"], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        print(SETUP_HELP.format(pip=pip), flush=True)
        return 3
    cmd = [py, "-m", "publisher", "publish"] + rest
    print("> " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["refresh", "collect", "queue", "check", "serve", "publish", "drive-links"])
    p.add_argument("--scheme", help="scheme id (collect default: jsjb; check default: all)")
    p.add_argument("--port", type=int, default=8000)
    opts, rest = p.parse_known_args()

    if opts.command == "serve":
        print(f"Serving site/ at http://localhost:{opts.port}/", flush=True)
        return subprocess.call([sys.executable, "-m", "http.server", str(opts.port),
                                "--directory", os.path.join(ROOT, "site")])

    if opts.command == "publish":
        return publish(rest)

    if opts.command == "queue":
        return run("collector.queue_runner", rest)

    if opts.command == "drive-links":
        return run("publisher", ["drive-links"] + rest)

    if opts.command == "check":
        return run("checker", rest + (["--scheme", opts.scheme] if opts.scheme else []))

    collect_args, check_args = split_check_flags(rest)
    code = run("collector", [opts.scheme or "jsjb"] + collect_args)
    if opts.command == "collect":
        return code
    if code not in (0, 130):
        print(f"Collector stopped with exit code {code}; checking what was collected so far.", flush=True)
    check_code = run("checker", check_args)
    return code or check_code


if __name__ == "__main__":
    sys.exit(main())
