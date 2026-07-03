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
        # A module may opt out of the /watch registries by declaring RESOLVE_ONLY:
        # it wires itself into the /resolve client-offload grant instead (its bytes
        # never flow through the backend). See discover_resolve_grants below.
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


# Cache the descriptor sweep: the set of injected modules is fixed at process
# start, so scan the package once and reuse (build_report / admin snapshot / the
# grant registry all ask for it).
_grant_cache: dict[str, list] = {}


def discover_resolve_grants(package):
    """Return the ``RESOLVE_GRANT`` descriptors declared by injected modules in
    ``package`` (empty in a base build).

    A cookie/secret-bound source that delivers its bytes off-backend (the /resolve
    client-offload path, not the /watch registries) declares a module-level
    ``RESOLVE_GRANT`` dict describing how to wire it. This helper collects those so
    the public HTTP layer can build the grant registry, the admin flags and the
    config report **without naming any injected source**. A build without the
    overlay finds none, so the whole path stays dormant. Off via
    ``PRIVATE_SOURCES_ENABLED=0``.

    Each descriptor is a dict with keys:
      * ``keys``           — the /resolve ``source`` values it answers (tuple/list).
      * ``is_configured``  — callable() -> bool (the secret/env gate).
      * ``scraper``        — "package.module:ClassName" discovery ref.
      * ``resolver``       — "package.module:ClassName" discovery ref.
      * ``admin_flags``    — optional {flag_name: callable() -> bool} for the dashboard.
      * ``config_feature`` — optional (label, hint) for the startup config report.
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


def load_ref(ref: str):
    """Resolve a ``"package.module:ClassName"`` descriptor reference to the object.
    Used to load a grant's scraper/resolver class lazily (the descriptor carries
    strings so the injected resolver module needn't import its scraper, avoiding a
    circular import)."""
    module_path, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module_path), attr)
