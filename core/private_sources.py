"""Auto-discovery for the optional build-time source overlay.

An operator build drops extra modules into ``scrapers``, ``resolvers`` and
``manga_engine``; the registries find them here without naming them. A base
build finds nothing. ``PRIVATE_SOURCES_ENABLED=0`` turns discovery off.
"""

import importlib
import inspect
import logging
import pkgutil
from types import ModuleType
from typing import Iterator

from core.config import get_settings

logger = logging.getLogger(__name__)


def overlay_modules(package, skip=()) -> Iterator[ModuleType]:
    """Every importable overlay module in ``package``, minus ``_`` helpers, tests
    and ``skip``. One that fails to import is skipped: a dead source must not take
    boot down."""
    if not get_settings().private_sources_enabled:
        return
    for info in pkgutil.iter_modules(package.__path__):
        name = info.name
        if name in skip or name.startswith("_") or "test" in name:
            continue
        full_name = f"{package.__name__}.{name}"
        try:
            yield importlib.import_module(full_name)
        except Exception as exc:
            logger.warning("overlay module %s failed to import, skipping: %s", full_name, exc)


def discover_private_sources(package, base_class, public_modules):
    """``base_class`` subclasses defined (not merely imported) in overlay
    modules, so a class shared through a helper is not registered twice."""
    found: dict[str, type] = {}
    for module in overlay_modules(package, skip=public_modules):
        # RESOLVE_ONLY modules wire into the /resolve grant instead, so their
        # bytes never flow through the backend's /watch.
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


# The overlay is fixed at process start and three callers ask for it.
_grant_cache: dict[str, list] = {}


def discover_resolve_grants(package):
    """The ``RESOLVE_GRANT`` descriptors declared by overlay modules in ``package``.

    | Key | Meaning |
    | --- | --- |
    | ``keys`` | the /resolve ``source`` values it answers |
    | ``is_configured`` | callable() -> bool, the secret gate |
    | ``scraper`` / ``resolver`` | ``"package.module:ClassName"`` refs |
    | ``admin_flags`` | optional {name: callable() -> bool} for the dashboard |
    | ``config_feature`` | optional (label, hint) for the startup report |
    """
    key = package.__name__
    if key not in _grant_cache:
        grants = [
            desc for module in overlay_modules(package)
            if isinstance(desc := getattr(module, "RESOLVE_GRANT", None), dict) and desc.get("keys")
        ]
        if grants:
            logger.info("registered %d resolve-grant source(s)", len(grants))
        _grant_cache[key] = grants
    return _grant_cache[key]


def discover_manga_provider(package):
    for module in overlay_modules(package):
        provider = getattr(module, "MANGA_PROVIDER", None)
        if provider is not None:
            logger.info("registered injected manga provider: %s", module.__name__)
            return provider
    return None


def load_ref(ref: str):
    """Resolve ``"package.module:ClassName"``. Grants carry strings because an
    overlay resolver importing its scraper would be circular."""
    module_path, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module_path), attr)
