# core/private_sources.py
#
# Auto-discovery for the optional build-time source overlay.
#
# An operator build may drop extra source modules into the ``scrapers`` and
# ``resolvers`` packages (see the self-hosting docs). This helper finds those
# modules and returns their source classes so the registries can append them — with
# no edit to the committed registries. A build without the overlay finds nothing, so
# discovery returns ``[]``. Off via ``PRIVATE_SOURCES_ENABLED=0``.
import importlib
import inspect
import logging
import os
import pkgutil

logger = logging.getLogger(__name__)


def discover_private_sources(package, base_class, public_modules):
    """Return the injected (private) source classes found in ``package``.

    A module is considered an injected source when it is NOT one of the known
    public/operator-owned modules, is not a private helper (``_``-prefixed) and is
    not a test module. Within each such module we collect every concrete subclass
    of ``base_class`` that is *defined there* (so a class merely imported from a
    shared helper isn't double-registered) and whose name isn't ``_``-prefixed (so
    ``_``-prefixed abstract bases are skipped). Modules that fail to import are
    logged and skipped rather than taking the whole registry down.
    """
    if os.getenv("PRIVATE_SOURCES_ENABLED", "1") == "0":
        return []

    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(package.__path__):
        name = info.name
        if name in public_modules or name.startswith("_") or "test" in name:
            continue
        full_name = f"{package.__name__}.{name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001 - a dead/legacy source must not break boot
            logger.warning("private source %s failed to import, skipping: %s", full_name, exc)
            continue
        for cls_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, base_class)
                and obj is not base_class
                and not cls_name.startswith("_")
                and obj.__module__ == module.__name__
            ):
                found[f"{obj.__module__}.{cls_name}"] = obj

    if found:
        logger.info("registered %d private source(s): %s", len(found), ", ".join(sorted(found)))
    return list(found.values())
