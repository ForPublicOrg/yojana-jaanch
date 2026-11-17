"""Scheme adapters, found by looking at the files in this folder.

Every collector/schemes/<name>.py that defines a SchemeAdapter subclass with an id is registered
under that id; nothing else needs editing. Files whose name starts with "_" are left out. A file
that fails to import (for example one still being written) is left out with a warning, so the
other schemes keep working; BROKEN holds the reason."""
import importlib
import inspect
import logging
import pkgutil

from ..base import SchemeAdapter

log = logging.getLogger("collector.schemes")


def scan(paths, package):
    """({id: adapter class}, {module name: why it was left out}) for the modules in paths."""
    found, broken = {}, {}
    for info in sorted(pkgutil.iter_modules(paths), key=lambda m: m.name):
        if info.name.startswith("_") or info.ispkg:
            continue
        try:
            module = importlib.import_module(f"{package}.{info.name}")
        except Exception as e:
            broken[info.name] = f"{type(e).__name__}: {e}"
            log.warning("scheme adapter %s.py left out: it does not import (%s)", info.name, broken[info.name])
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if not (issubclass(cls, SchemeAdapter) and cls is not SchemeAdapter
                    and cls.__module__ == module.__name__ and getattr(cls, "id", "")):
                continue
            if cls.id in found:
                broken[info.name] = f"scheme id {cls.id!r} is already used by {found[cls.id].__module__}"
                log.warning("scheme adapter %s.py left out: %s", info.name, broken[info.name])
                continue
            found[cls.id] = cls
    return found, broken


SCHEMES, BROKEN = scan(__path__, __name__)
