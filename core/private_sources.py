# Auto-discovery for the optional build-time source overlay.
#
# An operator build may drop extra source modules into ``scrapers`` and
# ``resolvers``. These helpers find them so the registries can append them with no
# edit to the committed registries. A base build finds nothing and they return
# empty. Off via ``PRIVATE_SOURCES_ENABLED=0``.
import importlib
import inspect
import logging
import os
import pkgutil

logger = logging.getLogger(__name__)


def discover_private_sources(package, base_class, public_modules):
    """The injected source classes found in ``package``.

    A module counts as injected when it is not a known public module, a
    ``_``-prefixed helper or a test. From each, every concrete ``base_class``
    subclass *defined there* is collected, so a class merely imported from a
    shared helper is not double-registered. A module that fails to import is
    logged and skipped rather than taking the registry down.
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
        except Exception as exc:  # noqa: BLE001 - a dead source must not break boot
            logger.warning("private source %s failed to import, skipping: %s", full_name, exc)
            continue
        # RESOLVE_ONLY opts a module out of the /watch registries: it wires into
        # the /resolve grant instead, so its bytes never flow through the backend.
        if getattr(module, "RESOLVE_ONLY", False):
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


# The set of injected modules is fixed at process start, and three callers ask
# for it, so scan the package once and reuse.
_grant_cache: dict[str, list] = {}


def discover_resolve_grants(package):
    """The ``RESOLVE_GRANT`` descriptors declared by injected modules in ``package``.

    A secret-bound source that delivers its bytes off-backend declares a
    module-level ``RESOLVE_GRANT`` dict. Collecting them here lets the public HTTP
    layer build the grant registry, admin flags and config report without naming
    any injected source. A base build finds none, so the path stays dormant.

    Each descriptor holds:
      * ``keys``           the /resolve ``source`` values it answers
      * ``is_configured``  callable() -> bool, the secret gate
      * ``scraper``        "package.module:ClassName" ref
      * ``resolver``       "package.module:ClassName" ref
      * ``admin_flags``    optional {name: callable() -> bool} for the dashboard
      * ``config_feature`` optional (label, hint) for the startup report
    """
    if os.getenv("PRIVATE_SOURCES_ENABLED", "1") == "0":
        return []
    key = package.__name__
    if key in _grant_cache:
        return _grant_cache[key]

    out: list[dict] = []
    for info in pkgutil.iter_modules(package.__path__):
        name = info.name
        if name.startswith("_") or "test" in name:
            continue
        try:
            module = importlib.import_module(f"{key}.{name}")
        except Exception as exc:  # noqa: BLE001 - a dead overlay must not break boot
            logger.warning("resolve-grant module %s.%s failed to import: %s", key, name, exc)
            continue
        desc = getattr(module, "RESOLVE_GRANT", None)
        if isinstance(desc, dict) and desc.get("keys"):
            out.append(desc)

    if out:
        logger.info("registered %d resolve-grant source(s)", len(out))
    _grant_cache[key] = out
    return out


def discover_manga_provider(package):
    """The injected manga provider instance from an overlay module, else ``None``.

    The public backend ships no manga source and never talks to a manga host. An
    operator build may drop in a module declaring a ``MANGA_PROVIDER`` instance;
    this returns the first one, so the public code names no provider.
    """
    if os.getenv("PRIVATE_SOURCES_ENABLED", "1") == "0":
        return None
    for info in pkgutil.iter_modules(package.__path__):
        name = info.name
        if name.startswith("_") or "test" in name:
            continue
        try:
            module = importlib.import_module(f"{package.__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 - a dead overlay must not break boot
            logger.warning("manga provider %s.%s failed to import: %s", package.__name__, name, exc)
            continue
        provider = getattr(module, "MANGA_PROVIDER", None)
        if provider is not None:
            logger.info("registered injected manga provider: %s.%s", package.__name__, name)
            return provider
    return None


def load_ref(ref: str):
    """Resolve a ``"package.module:ClassName"`` reference to the object. Grants
    carry strings so an injected resolver needn't import its scraper, which would
    be a circular import."""
    module_path, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module_path), attr)
