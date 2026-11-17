import sys

USAGE = "usage: python -m publisher {publish,drive-links,phash} [options]"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("publish", "drive-links", "phash"):
        print(USAGE)
        return 2
    if argv[0] == "publish":
        from .publish import main as run
    elif argv[0] == "phash":
        from .phash import main as run
    else:
        from .drive import main as run
    return run(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
