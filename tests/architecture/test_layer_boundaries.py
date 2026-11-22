"""Zero-external-dependency AST architecture and layering guardrails.

Enforces the target backend layered DAG (docs/design/layered-architecture-refactor.md §3.1 & §7.3):
- L0 ``core`` -> stdlib + third-party only
- L1 ``infra`` -> ``core`` + ``infra`` (plus internal subdomain hygiene)
- L2 ``persona`` -> ``core`` + ``infra`` + ``persona``
- L3 ``operations`` -> ``core`` + ``infra`` + ``persona`` + ``operations`` (no ``discovery <-> inbox``)
- L3 ``live`` -> ``core`` + ``infra`` + ``persona`` + ``live``
- L3 ``agent`` -> ``core`` + ``infra`` + ``persona`` + ``agent`` + ``operations.facade`` + ``live.facade``
- L4 ``app`` -> never imported by L0-L3
- Root cleanliness (only ``__init__.py`` / ``__main__.py`` plus explicit finite M2/M3 legacy pending sets)
- Credential boundary (``bilibili-credentials.json`` confined to ``infra/platforms/bilibili/account`` or tests)
- Tarjan SCC cycle detection at module and subpackage granularity
- Test isolation (no ``test_*.py -> test_*.py`` cross-imports inside migrated test directories)
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "tuber_studio"
TESTS_ROOT = REPO_ROOT / "tests"
PACKAGE_NAME = "tuber_studio"

ROOT_ENTRYPOINT_FILES = frozenset({"__init__.py", "__main__.py"})
TARGET_LAYER_PACKAGES = frozenset({"core", "infra", "persona", "operations", "live", "agent", "app"})

# Explicit finite set of root .py files slated for M2 (operations/) and M3 (app/) per #7.
# No new .py file may ever be added to src/tuber_studio/ root.
# All 21 M2-A operations kernel and channel root modules are now migrated.
LEGACY_ROOT_PENDING_M2 = frozenset()
LEGACY_ROOT_PENDING_M3 = frozenset(
    {
        "app.py",
        "meme_api.py",
        "message_api.py",
        "operation_recovery_api.py",
        "runtime.py",
        "web_entry.py",
    }
)
LEGACY_ROOT_PENDING_M2_M3 = LEGACY_ROOT_PENDING_M2 | LEGACY_ROOT_PENDING_M3

# M1 root files pending migration into core/, infra/, persona/.
# Only consulted before M1 begins (when none of core/, infra/, persona/ exist yet).
LEGACY_ROOT_PENDING_M1 = frozenset(
    {
        # L0 core
        "auth_sessions.py",
        "config.py",
        "deployment_profiles.py",
        "generation_meter.py",
        "inference_validation.py",
        "models.py",
        "operation_identity.py",
        "operation_text.py",
        "publication_errors.py",
        "retention.py",
        "runtime_authority.py",
        "sse.py",
        "store.py",
        # L1 infra
        "api_providers.py",
        "bilibili_account.py",
        "http_api.py",
        "media_cache.py",
        "omni_speech.py",
        "provider_diagnostics.py",
        "provider_settings.py",
        "providers.py",
        "responses_provider.py",
        "video_vision.py",
        "video_vision_service.py",
        "web_fetch.py",
        "web_search.py",
        # L2 persona
        "agent_tools.py",
        "artwork_references.py",
        "character_presets.py",
        "compaction.py",
        "context.py",
        "context_delivery.py",
        "cooperation.py",
        "daily_memory.py",
        "front_background.py",
        "front_persona.py",
        "gallery_search.py",
        "illustration_policy.py",
        "meme_generation.py",
        "meme_gif.py",
        "meme_selection.py",
        "memes.py",
        "memory.py",
        "memory_sources.py",
        "performance.py",
        "pet_appearance.py",
        "relationships.py",
        "subtitle_presentation.py",
        "workstation_images.py",
    }
)

# Explicit finite allowlist for pre-M3 un-migrated live/agent internals ONLY.
# ZERO entries are allowed for core/, infra/, persona/, operations/, or agent -> operations.
LEGACY_CROSS_ALLOWLIST = frozenset(
    {
        # M3 live -> agent / app_legacy
        ("tuber_studio.live.api", "tuber_studio.web_entry"),
        ("tuber_studio.live.stardew_tools", "tuber_studio.agent.task_runtime"),
        # M3 agent -> live / app_legacy (pending live.facade / app M3 wiring)
        ("tuber_studio.agent.private_call_guard", "tuber_studio.web_entry"),
        ("tuber_studio.agent.task_tools", "tuber_studio.live.game_livestream"),
        ("tuber_studio.agent.workshop_settings", "tuber_studio.live.media"),
        ("tuber_studio.agent.workshop_settings", "tuber_studio.live.options"),
    }
)

# Pre-M3 module-level cycles inside un-split live/agent internals only.
LEGACY_L3_MODULE_CYCLE_ALLOWLIST = frozenset(
    {
        frozenset(
            {
                "tuber_studio.agent.clip_refinement",
                "tuber_studio.agent.recall_memory",
                "tuber_studio.agent.task_delegation",
                "tuber_studio.agent.task_delivery",
            }
        ),
        frozenset({"tuber_studio.live.media", "tuber_studio.live.singing"}),
        frozenset({"tuber_studio.live.stardew_device", "tuber_studio.live.stardew_tools"}),
    }
)

PRE_M3_SUBPACKAGE_CYCLE_NODES = frozenset(
    {
        f"{PACKAGE_NAME}.live",
        f"{PACKAGE_NAME}.agent",
        f"{PACKAGE_NAME}.app_legacy",
    }
)

CREDENTIAL_FILENAME = "bilibili-credentials.json"
ALLOWED_CREDENTIAL_MODULES = frozenset({"tuber_studio.infra.platforms.bilibili.account"})

MIGRATED_TEST_DIRS = (
    "architecture",
    "support",
    "core",
    "infra",
    "persona",
    "operations",
    "live",
    "agent",
    "app",
    "e2e",
)

_OPERATIONS_LEGACY_MODULES = frozenset(name.removesuffix(".py") for name in LEGACY_ROOT_PENDING_M2)
_APP_LEGACY_MODULES = frozenset(name.removesuffix(".py") for name in LEGACY_ROOT_PENDING_M3)
_M1_LEGACY_MODULES = frozenset(name.removesuffix(".py") for name in LEGACY_ROOT_PENDING_M1)
_M1_TARGET_SUBLAYERS = frozenset({"core", "infra", "persona"})


@dataclass(frozen=True)
class ModuleImport:
    """Resolved internal Python import edge extracted statically from an AST."""

    source_module: str
    target_module: str
    lineno: int
    top_level: bool
    rel_path: str


def _is_m1_started(src_pkg: Path) -> bool:
    return any((src_pkg / layer).is_dir() for layer in _M1_TARGET_SUBLAYERS)


def _is_m2_pending(src_pkg: Path) -> bool:
    return (src_pkg / "operations" / "browsing").is_dir() or any(
        (src_pkg / fname).exists() for fname in LEGACY_ROOT_PENDING_M2
    )


def _discover_package(src_pkg: Path, package_name: str = PACKAGE_NAME) -> tuple[dict[str, Path], set[str]]:
    modules: dict[str, Path] = {}
    dirs: set[str] = {package_name}
    if not src_pkg.is_dir():
        return modules, dirs

    for path in sorted(src_pkg.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src_pkg)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mod_name = ".".join([package_name, *parts])
        modules[mod_name] = path

    for directory in sorted(src_pkg.rglob("*")):
        if directory.is_dir() and "__pycache__" not in directory.parts:
            rel_parts = directory.relative_to(src_pkg).parts
            dirs.add(".".join([package_name, *rel_parts]))

    return modules, dirs


def _top_level_stmt_ids(tree: ast.Module) -> set[int]:
    """Return AST node ids for module-level statements executed on import."""
    ids: set[int] = set()

    def visit(stmts: Iterable[ast.stmt]) -> None:
        for stmt in stmts:
            ids.add(id(stmt))
            if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
                for attr in ("body", "orelse", "finalbody"):
                    child_stmts = getattr(stmt, attr, None)
                    if isinstance(child_stmts, list):
                        visit(child_stmts)
                for handler in getattr(stmt, "handlers", ()):
                    visit(handler.body)

    visit(tree.body)
    return ids


def _resolve_import_from(
    source_module: str,
    is_pkg_init: bool,
    node: ast.ImportFrom,
    known_modules: set[str],
    known_dirs: set[str],
    package_name: str,
) -> tuple[list[str], bool]:
    """Resolve an ast.ImportFrom into canonical package.* targets."""
    pkg_parts = source_module.split(".") if is_pkg_init else source_module.split(".")[:-1]
    mod = node.module or ""

    if node.level > 0:
        up = node.level - 1
        if up > len(pkg_parts):
            return [], True
        base = pkg_parts[: len(pkg_parts) - up]
        full = ".".join((*base, mod) if mod else base)
    else:
        full = mod

    if not (full == package_name or full.startswith(f"{package_name}.")):
        return [], False

    targets: list[str] = []
    for alias in node.names:
        if alias.name == "*":
            targets.append(full)
            continue
        candidate = f"{full}.{alias.name}" if full else alias.name
        is_known_child = candidate in known_modules or candidate in known_dirs
        is_implicit_root_child = full == package_name and (
            alias.name in TARGET_LAYER_PACKAGES
            or alias.name in _OPERATIONS_LEGACY_MODULES
            or alias.name in _APP_LEGACY_MODULES
            or alias.name in _M1_LEGACY_MODULES
        )
        targets.append(candidate if (is_known_child or is_implicit_root_child) else full)
    return targets, False


def extract_module_imports(
    src_pkg: Path,
    package_name: str = PACKAGE_NAME,
) -> tuple[dict[str, Path], set[str], list[ModuleImport], list[str]]:
    """Parse all Python modules under src_pkg and resolve internal imports statically."""
    modules, dirs = _discover_package(src_pkg, package_name=package_name)
    known_modules = set(modules)
    imports: list[ModuleImport] = []
    errors: list[str] = []

    for mod_name, path in sorted(modules.items()):
        rel_path = path.relative_to(src_pkg).as_posix()
        is_pkg_init = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_ids = _top_level_stmt_ids(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                is_top = id(node) in top_ids
                for alias in node.names:
                    if alias.name == package_name or alias.name.startswith(f"{package_name}."):
                        imports.append(
                            ModuleImport(
                                source_module=mod_name,
                                target_module=alias.name,
                                lineno=node.lineno,
                                top_level=is_top,
                                rel_path=rel_path,
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                is_top = id(node) in top_ids
                targets, invalid_rel = _resolve_import_from(
                    mod_name, is_pkg_init, node, known_modules, dirs, package_name
                )
                if invalid_rel:
                    errors.append(
                        f"{rel_path}:{node.lineno}: relative import level={node.level} escapes {package_name}"
                    )
                    continue
                for target in targets:
                    imports.append(
                        ModuleImport(
                            source_module=mod_name,
                            target_module=target,
                            lineno=node.lineno,
                            top_level=is_top,
                            rel_path=rel_path,
                        )
                    )

    return modules, dirs, imports, errors


def _classify_layer(module: str, *, allow_m1_pending: bool) -> str:
    if module in {PACKAGE_NAME, f"{PACKAGE_NAME}.__main__"}:
        return "entrypoint"
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE_NAME:
        return "external"
    top = parts[1]
    if top in TARGET_LAYER_PACKAGES:
        return top
    if top in _OPERATIONS_LEGACY_MODULES:
        return "operations_legacy"
    if top in _APP_LEGACY_MODULES or top == "model_runtime":
        return "app_legacy"
    if allow_m1_pending and (top == "platforms" or top in _M1_LEGACY_MODULES):
        return "m1_pending"
    return "unknown"


def _infra_subdomain(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == PACKAGE_NAME and parts[1] == "infra":
        return parts[2]
    return None


def _operations_subdomain(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == PACKAGE_NAME and parts[1] == "operations":
        return parts[2]
    return None


def _is_facade_module(module: str, domain: str) -> bool:
    prefix = f"{PACKAGE_NAME}.{domain}.facade"
    return module == prefix or module.startswith(f"{prefix}.")


def _check_infra_hygiene(imp: ModuleImport) -> str | None:
    src_sub = _infra_subdomain(imp.source_module)
    dst_sub = _infra_subdomain(imp.target_module)
    if not src_sub or not dst_sub or src_sub == dst_sub:
        return None
    if src_sub in {"inference", "web_client", "media"}:
        return (
            f"{imp.rel_path}:{imp.lineno}: infra/{src_sub} must not import infra/{dst_sub} "
            f"({imp.target_module})"
        )
    if src_sub == "platforms":
        if dst_sub in {"web_client", "media"}:
            return (
                f"{imp.rel_path}:{imp.lineno}: infra/platforms must not import infra/{dst_sub} "
                f"({imp.target_module})"
            )
        if dst_sub == "inference" and not (
            imp.target_module == f"{PACKAGE_NAME}.infra.inference.validation"
            or imp.target_module.startswith(f"{PACKAGE_NAME}.infra.inference.validation.")
        ):
            return (
                f"{imp.rel_path}:{imp.lineno}: infra/platforms must not import infra/inference "
                f"gateway module {imp.target_module}"
            )
    return None


def check_root_cleanliness(src_pkg: Path, *, allow_m1_pending: bool | None = None) -> list[str]:
    """Verify no unallowlisted .py modules or unexpected packages exist in src/tuber_studio/."""
    if allow_m1_pending is None:
        allow_m1_pending = not _is_m1_started(src_pkg)

    allowed_root = set(ROOT_ENTRYPOINT_FILES) | LEGACY_ROOT_PENDING_M2_M3
    if allow_m1_pending:
        allowed_root |= LEGACY_ROOT_PENDING_M1

    violations: list[str] = []
    if not src_pkg.is_dir():
        return [f"Missing package directory: {src_pkg}"]

    for py_file in sorted(src_pkg.glob("*.py")):
        if py_file.name not in allowed_root:
            violations.append(
                f"Disallowed root module src/tuber_studio/{py_file.name}; "
                "only __init__.py, __main__.py, and explicit pending legacy modules are allowed"
            )

    allowed_pkgs = set(TARGET_LAYER_PACKAGES)
    if not (src_pkg / "app").is_dir():
        allowed_pkgs.add("model_runtime")
    if allow_m1_pending:
        allowed_pkgs.add("platforms")

    for py_file in sorted(src_pkg.rglob("*.py")):
        if "__pycache__" in py_file.parts or py_file.parent == src_pkg:
            continue
        top_dir = py_file.relative_to(src_pkg).parts[0]
        if top_dir not in allowed_pkgs:
            rel = py_file.relative_to(src_pkg).as_posix()
            violations.append(f"Disallowed top-level Python package directory for {rel}")

    return violations


def check_layer_dag(
    src_pkg: Path,
    *,
    allow_m1_pending: bool | None = None,
    cross_allowlist: frozenset[tuple[str, str]] = LEGACY_CROSS_ALLOWLIST,
) -> list[str]:
    """Enforce strict L0-L4 DAG, domain isolation, discovery<->inbox isolation, and agent facades."""
    if allow_m1_pending is None:
        allow_m1_pending = not _is_m1_started(src_pkg)

    _, _, imports, parse_errors = extract_module_imports(src_pkg)
    violations: list[str] = list(parse_errors)

    for imp in imports:
        if imp.source_module == imp.target_module:
            continue
        src_layer = _classify_layer(imp.source_module, allow_m1_pending=allow_m1_pending)
        dst_layer = _classify_layer(imp.target_module, allow_m1_pending=allow_m1_pending)

        if src_layer == "entrypoint":
            if dst_layer not in {"entrypoint", "core", "app", "app_legacy"}:
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: root entrypoint imports {imp.target_module} ({dst_layer})"
                )
            continue

        if src_layer == "core":
            if dst_layer != "core":
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L0 core cannot import {imp.target_module} ({dst_layer})"
                )
            continue

        if src_layer == "infra":
            if dst_layer not in {"core", "infra"}:
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L1 infra cannot import {imp.target_module} ({dst_layer})"
                )
                continue
            hygiene_err = _check_infra_hygiene(imp)
            if hygiene_err:
                violations.append(hygiene_err)
            continue

        if src_layer == "persona":
            if dst_layer not in {"core", "infra", "persona"}:
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L2 persona cannot import {imp.target_module} ({dst_layer})"
                )
            continue

        if src_layer in {"operations", "operations_legacy"}:
            src_sub = _operations_subdomain(imp.source_module)
            dst_sub = _operations_subdomain(imp.target_module)
            if {src_sub, dst_sub} == {"discovery", "inbox"}:
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: operations/{src_sub} must never import "
                    f"operations/{dst_sub} ({imp.target_module})"
                )
                continue
            allowed_dst = {"core", "infra", "persona", "operations", "operations_legacy"}
            if allow_m1_pending:
                allowed_dst.add("m1_pending")
            if dst_layer not in allowed_dst and (imp.source_module, imp.target_module) not in cross_allowlist:
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L3 operations cannot import "
                    f"{imp.target_module} ({dst_layer})"
                )
            continue

        if src_layer == "live":
            allowed_dst = {"core", "infra", "persona", "live"}
            if allow_m1_pending:
                allowed_dst.add("m1_pending")
            if dst_layer not in allowed_dst and (imp.source_module, imp.target_module) not in cross_allowlist:
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L3 live cannot import {imp.target_module} ({dst_layer})"
                )
            continue

        if src_layer == "agent":
            allowed_dst = {"core", "infra", "persona", "agent"}
            if allow_m1_pending:
                allowed_dst.add("m1_pending")
            via_facade = _is_facade_module(imp.target_module, "operations") or _is_facade_module(
                imp.target_module, "live"
            )
            if (
                dst_layer not in allowed_dst
                and not via_facade
                and (imp.source_module, imp.target_module) not in cross_allowlist
            ):
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L3 agent may only access operations/live via facade, "
                    f"not {imp.target_module} ({dst_layer})"
                )
            continue

        if src_layer in {"app", "app_legacy"}:
            if dst_layer == "unknown":
                violations.append(
                    f"{imp.rel_path}:{imp.lineno}: L4 app imports unknown module {imp.target_module}"
                )
            continue

        if src_layer == "unknown":
            violations.append(f"{imp.rel_path}:{imp.lineno}: unlayered module {imp.source_module}")

    return violations


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                ids.add(id(node.body[0].value))
    return ids


def check_credential_boundary(src_pkg: Path, *, allow_m1_pending: bool | None = None) -> list[str]:
    """Ensure only infra/platforms/bilibili/account (or pre-M1 comments.py) references credential JSON."""
    if allow_m1_pending is None:
        allow_m1_pending = not _is_m1_started(src_pkg)

    allowed_holders = set(ALLOWED_CREDENTIAL_MODULES)
    if allow_m1_pending:
        allowed_holders.add(f"{PACKAGE_NAME}.comments")

    modules, _ = _discover_package(src_pkg)
    violations: list[str] = []

    for mod_name, path in sorted(modules.items()):
        if any(mod_name == holder or mod_name.startswith(f"{holder}.") for holder in allowed_holders):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc_ids = _docstring_node_ids(tree)
        rel_path = path.relative_to(src_pkg).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in doc_ids
                and CREDENTIAL_FILENAME in node.value
            ):
                violations.append(
                    f"{rel_path}:{node.lineno}: {mod_name} must not reference {CREDENTIAL_FILENAME!r} directly"
                )

    return violations


def _tarjan_sccs(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[frozenset[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal counter
        indices[node] = counter
        lowlink[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])

        if lowlink[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                sccs.append(frozenset(component))

    for vertex in sorted(graph):
        if vertex not in indices:
            strongconnect(vertex)

    return sorted(sccs, key=lambda comp: sorted(comp))


def _subpackage_node(module: str, known_dirs: set[str], *, allow_m1_pending: bool) -> str:
    layer = _classify_layer(module, allow_m1_pending=allow_m1_pending)
    parts = module.split(".")
    if layer in {"core", "app"}:
        return f"{PACKAGE_NAME}.{layer}"
    if layer in {"infra", "persona"}:
        return ".".join(parts[:3]) if len(parts) >= 3 else f"{PACKAGE_NAME}.{layer}"
    if layer in {"operations", "live", "agent"}:
        if len(parts) >= 3 and ".".join(parts[:3]) in known_dirs:
            return ".".join(parts[:3])
        return f"{PACKAGE_NAME}.{layer}"
    if layer == "operations_legacy":
        return f"{PACKAGE_NAME}.operations_legacy"
    if layer == "app_legacy":
        return f"{PACKAGE_NAME}.app_legacy"
    if layer == "m1_pending":
        return f"{PACKAGE_NAME}.m1_pending"
    return module


def _is_m3_pending(src_pkg: Path) -> bool:
    return any((src_pkg / fname).exists() for fname in LEGACY_ROOT_PENDING_M3)


def check_cycles(
    src_pkg: Path,
    *,
    allow_m1_pending: bool | None = None,
    m3_pending: bool | None = None,
) -> list[str]:
    """Run Tarjan SCC cycle detection at top-level, module, and subpackage levels."""
    if allow_m1_pending is None:
        allow_m1_pending = not _is_m1_started(src_pkg)
    if m3_pending is None:
        m3_pending = _is_m3_pending(src_pkg)

    modules, dirs, imports, parse_errors = extract_module_imports(src_pkg)
    violations: list[str] = list(parse_errors)
    known_modules = set(modules)

    # 1. Top-level import module cycles across the entire package: ZERO cycles allowed.
    top_graph: dict[str, set[str]] = {mod: set() for mod in known_modules}
    for imp in imports:
        if imp.top_level and imp.target_module in known_modules and imp.target_module != imp.source_module:
            top_graph[imp.source_module].add(imp.target_module)
    for scc in _tarjan_sccs(top_graph):
        violations.append(f"Top-level import cycle detected: {sorted(scc)}")

    # 2. All-import (including lazy) module cycles.
    # Intra-bilibili lazy helper imports inside infra/platforms/bilibili are checked at top-level
    # and cross-subpackage levels; all other migrated layers and L3 modules are checked strictly.
    all_mod_graph: dict[str, set[str]] = {mod: set() for mod in known_modules}
    for imp in imports:
        if imp.target_module not in known_modules or imp.target_module == imp.source_module:
            continue
        both_bilibili = (
            imp.source_module.startswith(f"{PACKAGE_NAME}.infra.platforms.bilibili.")
            and imp.target_module.startswith(f"{PACKAGE_NAME}.infra.platforms.bilibili.")
        ) or (
            imp.source_module.startswith(f"{PACKAGE_NAME}.platforms.")
            and imp.target_module.startswith(f"{PACKAGE_NAME}.platforms.")
        )
        if not imp.top_level and both_bilibili:
            continue
        all_mod_graph[imp.source_module].add(imp.target_module)

    allowed_mod_cycles: set[frozenset[str]] = set()
    if m3_pending:
        allowed_mod_cycles |= LEGACY_L3_MODULE_CYCLE_ALLOWLIST

    for scc in _tarjan_sccs(all_mod_graph):
        if scc not in allowed_mod_cycles:
            violations.append(f"Module dependency cycle detected: {sorted(scc)}")

    # 3. Subpackage/component all-import SCCs.
    subpkg_graph: dict[str, set[str]] = {}
    for mod in known_modules:
        subpkg_graph.setdefault(_subpackage_node(mod, dirs, allow_m1_pending=allow_m1_pending), set())
    for imp in imports:
        if imp.target_module not in known_modules:
            continue
        src_sp = _subpackage_node(imp.source_module, dirs, allow_m1_pending=allow_m1_pending)
        dst_sp = _subpackage_node(imp.target_module, dirs, allow_m1_pending=allow_m1_pending)
        if src_sp != dst_sp:
            subpkg_graph.setdefault(src_sp, set()).add(dst_sp)

    for scc in _tarjan_sccs(subpkg_graph):
        if m3_pending and scc <= PRE_M3_SUBPACKAGE_CYCLE_NODES:
            continue
        violations.append(f"Subpackage dependency cycle detected: {sorted(scc)}")

    return violations


def _is_test_module_segment(name: str) -> bool:
    return any(part.startswith("test_") for part in name.split("."))


def check_test_isolation(tests_root: Path) -> list[str]:
    """Disallow test_*.py -> test_*.py cross-imports inside migrated test directories."""
    if not tests_root.is_dir():
        return [f"Missing tests directory: {tests_root}"]

    fully_migrated = not any(tests_root.glob("test_*.py")) and not (tests_root / "browser").is_dir()
    if fully_migrated:
        target_files = sorted(p for p in tests_root.rglob("*.py") if "__pycache__" not in p.parts)
    else:
        target_files = []
        for subdir in MIGRATED_TEST_DIRS:
            subpath = tests_root / subdir
            if subpath.is_dir():
                target_files.extend(p for p in subpath.rglob("*.py") if "__pycache__" not in p.parts)
        target_files = sorted(set(target_files))

    violations: list[str] = []
    for path in target_files:
        rel_path = path.relative_to(tests_root).as_posix()
        rel_parts = path.relative_to(tests_root).with_suffix("").parts
        pkg_parts = rel_parts[:-1] if path.name != "__init__.py" else rel_parts
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top.startswith("test_") or (top == "tests" and _is_test_module_segment(alias.name)):
                        violations.append(
                            f"{rel_path}:{node.lineno}: cross-test import `import {alias.name}` is forbidden"
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level > 0:
                    up = node.level - 1
                    base = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else ()
                    resolved = ".".join((*base, mod) if mod else base)
                    if _is_test_module_segment(resolved) or any(
                        a.name.startswith("test_") for a in node.names
                    ):
                        violations.append(
                            f"{rel_path}:{node.lineno}: relative cross-test import from `{mod or '.'}` "
                            "is forbidden"
                        )
                else:
                    top = mod.split(".")[0] if mod else ""
                    imports_test_pkg = top.startswith("test_") or (
                        top == "tests" and _is_test_module_segment(mod)
                    )
                    imports_test_child = mod == "tests" or mod.startswith("tests.")
                    if imports_test_pkg or (
                        imports_test_child and any(a.name.startswith("test_") for a in node.names)
                    ):
                        names = ", ".join(a.name for a in node.names)
                        violations.append(
                            f"{rel_path}:{node.lineno}: cross-test import `from {mod} import {names}` "
                            "is forbidden"
                        )

    return violations


# ---------------------------------------------------------------------------
# Repository guardrail assertions
# ---------------------------------------------------------------------------


def test_allowlists_are_strictly_bounded_to_m2_m3():
    assert not _is_m2_pending(SRC_ROOT)
    assert not (SRC_ROOT / "operations" / "browsing").exists()
    assert len(LEGACY_ROOT_PENDING_M2) == 0
    assert len(LEGACY_ROOT_PENDING_M3) == 6
    assert LEGACY_ROOT_PENDING_M2_M3 == (LEGACY_ROOT_PENDING_M2 | LEGACY_ROOT_PENDING_M3)
    assert len(LEGACY_ROOT_PENDING_M1) == 49
    assert not (LEGACY_ROOT_PENDING_M2_M3 & LEGACY_ROOT_PENDING_M1)
    assert not (LEGACY_ROOT_PENDING_M2_M3 & ROOT_ENTRYPOINT_FILES)

    for src, dst in LEGACY_CROSS_ALLOWLIST:
        src_top = src.split(".")[1]
        assert src_top not in _M1_TARGET_SUBLAYERS, f"Forbidden M1 entry in LEGACY_CROSS_ALLOWLIST: {src}"
        assert not src.startswith(f"{PACKAGE_NAME}.operations"), f"Forbidden M2 entry: {src}"
        assert not dst.startswith(f"{PACKAGE_NAME}.operations"), f"Forbidden M2 target: {dst}"

    for comp in LEGACY_L3_MODULE_CYCLE_ALLOWLIST:
        for mod in comp:
            assert mod.split(".")[1] in {"live", "agent"}


def test_real_repo_root_cleanliness():
    violations = check_root_cleanliness(SRC_ROOT)
    assert not violations, "Root cleanliness violations:\n" + "\n".join(violations)


def test_real_repo_layer_dag_and_domain_isolation():
    violations = check_layer_dag(SRC_ROOT)
    assert not violations, "Layer DAG violations:\n" + "\n".join(violations)


def test_real_repo_credential_boundary():
    violations = check_credential_boundary(SRC_ROOT)
    assert not violations, "Credential boundary violations:\n" + "\n".join(violations)


def test_real_repo_no_unexpected_cycles():
    violations = check_cycles(SRC_ROOT)
    assert not violations, "Dependency cycle violations:\n" + "\n".join(violations)


def test_real_repo_test_isolation():
    violations = check_test_isolation(TESTS_ROOT)
    assert not violations, "Test isolation violations:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Mock-tree self-tests proving every violation category fails as intended
# ---------------------------------------------------------------------------


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _build_valid_mock_repo(tmp_path: Path) -> tuple[Path, Path]:
    _write_tree(
        tmp_path,
        {
            "src/tuber_studio/__init__.py": '"""Mock package."""\n',
            "src/tuber_studio/__main__.py": "from tuber_studio.app import create_app\n",
            "src/tuber_studio/core/__init__.py": "",
            "src/tuber_studio/core/models.py": "import pydantic\n",
            "src/tuber_studio/core/config.py": "from .models import *\n",
            "src/tuber_studio/infra/__init__.py": "",
            "src/tuber_studio/infra/inference/__init__.py": "",
            "src/tuber_studio/infra/inference/validation.py": "from ...core import config\n",
            "src/tuber_studio/infra/inference/providers.py": "from .validation import *\n",
            "src/tuber_studio/infra/platforms/__init__.py": "",
            "src/tuber_studio/infra/platforms/bilibili/__init__.py": "",
            "src/tuber_studio/infra/platforms/bilibili/account.py": (
                "from ....core import config\n"
                "from ...inference.validation import *\n"
                'CRED_PATH = "bilibili-credentials.json"\n'
            ),
            "src/tuber_studio/infra/platforms/bilibili/client.py": "from .account import CRED_PATH\n",
            "src/tuber_studio/infra/web_client/__init__.py": "",
            "src/tuber_studio/infra/web_client/fetch.py": "from ...core import models\n",
            "src/tuber_studio/infra/media/__init__.py": "",
            "src/tuber_studio/infra/media/cache.py": "from ...core import models\n",
            "src/tuber_studio/persona/__init__.py": "",
            "src/tuber_studio/persona/memory/__init__.py": "",
            "src/tuber_studio/persona/memory/daily.py": "from ...core import config\n",
            "src/tuber_studio/persona/characters/__init__.py": "",
            "src/tuber_studio/persona/characters/presets.py": (
                "from ...infra.platforms.bilibili import account\nfrom ..memory import daily\n"
            ),
            "src/tuber_studio/persona/context/__init__.py": "",
            "src/tuber_studio/persona/context/manager.py": "from ...infra.inference import providers\n",
            "src/tuber_studio/persona/assets/__init__.py": "",
            "src/tuber_studio/persona/assets/memes.py": "from ...infra.media import cache\n",
            "src/tuber_studio/operations/__init__.py": "",
            "src/tuber_studio/operations/kernel/__init__.py": "",
            "src/tuber_studio/operations/kernel/control.py": "from ...core import models\n",
            "src/tuber_studio/operations/channels/__init__.py": "",
            "src/tuber_studio/operations/channels/comments.py": "from ..kernel import control\n",
            "src/tuber_studio/operations/discovery/__init__.py": "",
            "src/tuber_studio/operations/discovery/runner.py": (
                "from ..kernel import control\nfrom ...persona.memory import daily\n"
            ),
            "src/tuber_studio/operations/inbox/__init__.py": "",
            "src/tuber_studio/operations/inbox/runner.py": "from ..channels import comments\n",
            "src/tuber_studio/operations/facade.py": (
                "from .discovery import runner as d\nfrom .inbox import runner as i\n"
            ),
            "src/tuber_studio/live/__init__.py": "",
            "src/tuber_studio/live/session.py": "from ..persona.context import manager\n",
            "src/tuber_studio/live/facade.py": "from .session import *\n",
            "src/tuber_studio/agent/__init__.py": "",
            "src/tuber_studio/agent/service.py": (
                "from ..core import models\n"
                "from ..infra.inference import providers\n"
                "from ..persona.characters import presets\n"
                "from ..operations import facade as ops_facade\n"
                "from ..live.facade import *\n"
            ),
            "src/tuber_studio/app/__init__.py": (
                "from . import routes\ndef create_app():\n    return None\n"
            ),
            "src/tuber_studio/app/routes.py": (
                "from ..agent import service\n"
                "from ..operations import facade\n"
                "from ..live import facade as live_facade\n"
            ),
            "tests/conftest.py": "import pytest\n",
            "tests/support/stores.py": "from tuber_studio.core import models\n",
            "tests/core/test_models.py": "import pytest_asyncio\nfrom tests.support import stores\n",
            "tests/infra/test_account.py": 'CRED = "bilibili-credentials.json"\n',
            "tests/persona/test_presets.py": "from tuber_studio.persona.characters import presets\n",
            "tests/architecture/test_dummy.py": "import ast\n",
            "tests/app/test_routes.py": "from tuber_studio.app import create_app\n",
        },
    )
    return tmp_path / "src" / "tuber_studio", tmp_path / "tests"


def test_mock_valid_layered_tree_passes(tmp_path):
    src_pkg, tests_root = _build_valid_mock_repo(tmp_path)
    assert check_root_cleanliness(src_pkg) == []
    assert check_layer_dag(src_pkg, cross_allowlist=frozenset()) == []
    assert check_credential_boundary(src_pkg) == []
    assert check_cycles(src_pkg) == []
    assert check_test_isolation(tests_root) == []


def test_mock_relative_and_absolute_import_resolution(tmp_path):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(
        src_pkg,
        {
            "core/abs_bad.py": "import tuber_studio.infra.inference.providers\n",
            "core/rel_bad.py": "from .. import persona\n",
            "core/deep_rel_bad.py": "from ...tuber_studio import app\n",
        },
    )
    _, _, imports, errors = extract_module_imports(src_pkg)
    assert errors == []
    resolved = {(imp.source_module, imp.target_module) for imp in imports}
    assert ("tuber_studio.core.abs_bad", "tuber_studio.infra.inference.providers") in resolved
    assert ("tuber_studio.core.rel_bad", "tuber_studio.persona") in resolved
    assert ("tuber_studio.core.deep_rel_bad", "tuber_studio.app") in resolved


@pytest.mark.parametrize(
    "import_stmt",
    [
        "import tuber_studio.infra.inference.providers\n",
        "from ..infra import providers\n",
        "from ..persona.memory import daily\n",
        "from ..operations import facade\n",
        "from ..live import session\n",
        "from ..agent import service\n",
        "from ..app import create_app\n",
        "from tuber_studio import comments\n",
    ],
)
def test_mock_core_forbids_higher_layers_and_pending_root(tmp_path, import_stmt):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {"core/bad.py": import_stmt, "comments.py": "X = 1\n"})
    violations = check_layer_dag(src_pkg, cross_allowlist=frozenset())
    assert any("core/bad.py:1:" in msg and "L0 core cannot import" in msg for msg in violations)


@pytest.mark.parametrize(
    ("rel_path", "import_stmt", "expected_fragment"),
    [
        ("infra/bad_persona.py", "from ..persona.memory import daily\n", "L1 infra cannot import"),
        ("infra/bad_ops.py", "from ..operations import facade\n", "L1 infra cannot import"),
        ("infra/bad_live.py", "from ..live import session\n", "L1 infra cannot import"),
        ("infra/bad_agent.py", "from ..agent import service\n", "L1 infra cannot import"),
        ("infra/bad_app.py", "from ..app import create_app\n", "L1 infra cannot import"),
        ("infra/bad_root.py", "from .. import comments\n", "L1 infra cannot import"),
        ("infra/inference/bad.py", "from ..platforms.bilibili import client\n", "infra/inference must not"),
        ("infra/inference/bad2.py", "from ..web_client import fetch\n", "infra/inference must not"),
        ("infra/inference/bad3.py", "from ..media import cache\n", "infra/inference must not"),
        ("infra/web_client/bad.py", "from ..inference import providers\n", "infra/web_client must not"),
        ("infra/web_client/bad2.py", "from ..media import cache\n", "infra/web_client must not"),
        ("infra/media/bad.py", "from ..platforms.bilibili import account\n", "infra/media must not"),
        ("infra/media/bad2.py", "from ..web_client import fetch\n", "infra/media must not"),
        (
            "infra/platforms/bilibili/bad_web.py",
            "from ...web_client import fetch\n",
            "infra/platforms must not",
        ),
        ("infra/platforms/bilibili/bad_med.py", "from ...media import cache\n", "infra/platforms must not"),
        (
            "infra/platforms/bilibili/bad_inf.py",
            "from ...inference import providers\n",
            "infra/platforms must not import infra/inference gateway module",
        ),
    ],
)
def test_mock_infra_forbids_persona_l3_l4_and_enforces_internal_hygiene(
    tmp_path, rel_path, import_stmt, expected_fragment
):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {rel_path: import_stmt, "comments.py": "X = 1\n"})
    violations = check_layer_dag(src_pkg, cross_allowlist=frozenset())
    assert any(rel_path in msg and expected_fragment in msg for msg in violations)


@pytest.mark.parametrize(
    "import_stmt",
    [
        "from ..operations import facade\n",
        "from ..live import session\n",
        "from ..agent import service\n",
        "from ..app import create_app\n",
        "from .. import dynamics\n",
    ],
)
def test_mock_persona_forbids_operations_live_agent_app(tmp_path, import_stmt):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {"persona/bad.py": import_stmt, "dynamics.py": "X = 1\n"})
    violations = check_layer_dag(src_pkg, cross_allowlist=frozenset())
    assert any("persona/bad.py:1:" in msg and "L2 persona cannot import" in msg for msg in violations)


@pytest.mark.parametrize(
    ("rel_path", "import_stmt", "expected_fragment"),
    [
        ("operations/bad_live.py", "from ..live import facade\n", "L3 operations cannot import"),
        ("operations/bad_agent.py", "from ..agent import service\n", "L3 operations cannot import"),
        ("operations/bad_app.py", "from ..app import create_app\n", "L3 operations cannot import"),
        (
            "operations/discovery/bad_rel.py",
            "from ..inbox import runner\n",
            "operations/discovery must never import operations/inbox",
        ),
        (
            "operations/discovery/bad_abs.py",
            "import tuber_studio.operations.inbox.runner\n",
            "operations/discovery must never import operations/inbox",
        ),
        (
            "operations/inbox/bad_rel.py",
            "from ..discovery import runner\n",
            "operations/inbox must never import operations/discovery",
        ),
        (
            "operations/inbox/bad_abs.py",
            "import tuber_studio.operations.discovery.runner\n",
            "operations/inbox must never import operations/discovery",
        ),
    ],
)
def test_mock_operations_forbids_live_agent_app_and_discovery_inbox_crosstalk(
    tmp_path, rel_path, import_stmt, expected_fragment
):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {rel_path: import_stmt})
    violations = check_layer_dag(src_pkg, cross_allowlist=frozenset())
    assert any(rel_path in msg and expected_fragment in msg for msg in violations)


@pytest.mark.parametrize(
    "import_stmt",
    [
        "from ..operations import facade\n",
        "from ..agent import service\n",
        "from ..app import create_app\n",
    ],
)
def test_mock_live_forbids_operations_agent_app(tmp_path, import_stmt):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {"live/bad.py": import_stmt})
    violations = check_layer_dag(src_pkg, cross_allowlist=frozenset())
    assert any("live/bad.py:1:" in msg and "L3 live cannot import" in msg for msg in violations)


@pytest.mark.parametrize(
    "import_stmt",
    [
        "from ..operations.discovery import runner\n",
        "from ..operations.kernel import control\n",
        "from ..operations import internal_mod\n",
        "import tuber_studio.operations\n",
        "from ..live import session\n",
        "import tuber_studio.live\n",
        "from ..app import create_app\n",
        "from .. import comments\n",
    ],
)
def test_mock_agent_enforces_narrow_facades_only(tmp_path, import_stmt):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(
        src_pkg,
        {
            "operations/internal_mod.py": "X = 1\n",
            "agent/bad.py": import_stmt,
            "comments.py": "X = 1\n",
        },
    )
    violations = check_layer_dag(src_pkg, cross_allowlist=frozenset())
    assert any("agent/bad.py:1:" in msg and "L3 agent may only access" in msg for msg in violations)


def test_mock_root_cleanliness_rejects_new_and_unmigrated_files(tmp_path):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {"rogue_helper.py": "X = 1\n"})
    assert any("rogue_helper.py" in msg for msg in check_root_cleanliness(src_pkg, allow_m1_pending=True))
    assert any("rogue_helper.py" in msg for msg in check_root_cleanliness(src_pkg, allow_m1_pending=False))

    src_pkg2, _ = _build_valid_mock_repo(tmp_path / "m1")
    _write_tree(src_pkg2, {"config.py": "X = 1\n", "rogue_pkg/helper.py": "X = 1\n"})
    violations = check_root_cleanliness(src_pkg2)
    assert any("config.py" in msg for msg in violations)
    assert any("rogue_pkg/helper.py" in msg for msg in violations)


@pytest.mark.parametrize(
    "rel_path",
    [
        "core/bad_cred.py",
        "infra/platforms/bilibili/client.py",
        "persona/memory/bad_cred.py",
        "operations/inbox/bad_cred.py",
        "live/bad_cred.py",
        "agent/bad_cred.py",
        "app/bad_cred.py",
        "comments.py",
    ],
)
def test_mock_credential_boundary_rejects_non_account_modules(tmp_path, rel_path):
    src_pkg, _ = _build_valid_mock_repo(tmp_path)
    _write_tree(src_pkg, {rel_path: 'PATH = f"{root}/bilibili-credentials.json"\n'})
    violations = check_credential_boundary(src_pkg)
    assert any(rel_path in msg and CREDENTIAL_FILENAME in msg for msg in violations)


def test_mock_cycle_detection_catches_module_lazy_and_subpackage_cycles(tmp_path):
    src_pkg1, _ = _build_valid_mock_repo(tmp_path / "c1")
    _write_tree(
        src_pkg1,
        {
            "core/a.py": "from . import b\n",
            "core/b.py": "from . import a\n",
        },
    )
    v1 = check_cycles(src_pkg1)
    assert any("Top-level import cycle" in msg and "tuber_studio.core.a" in msg for msg in v1)

    src_pkg2, _ = _build_valid_mock_repo(tmp_path / "c2")
    _write_tree(
        src_pkg2,
        {
            "persona/memory/x.py": "def f():\n    from . import y\n",
            "persona/memory/y.py": "def g():\n    from . import z\n",
            "persona/memory/z.py": "def h():\n    from . import x\n",
        },
    )
    v2 = check_cycles(src_pkg2)
    assert any("Module dependency cycle" in msg and "tuber_studio.persona.memory.x" in msg for msg in v2)

    src_pkg3, _ = _build_valid_mock_repo(tmp_path / "c3")
    _write_tree(
        src_pkg3,
        {
            "persona/memory/from_char.py": "from ..characters import presets\n",
        },
    )
    v3 = check_cycles(src_pkg3)
    assert any(
        "Subpackage dependency cycle" in msg and "tuber_studio.persona.characters" in msg for msg in v3
    )

    src_pkg4, _ = _build_valid_mock_repo(tmp_path / "c4")
    _write_tree(
        src_pkg4,
        {
            "operations/kernel/back_to_channels.py": "from ..channels import comments\n",
        },
    )
    v4 = check_cycles(src_pkg4)
    assert any("Subpackage dependency cycle" in msg and "tuber_studio.operations.kernel" in msg for msg in v4)


@pytest.mark.parametrize(
    ("rel_path", "content"),
    [
        ("core/test_bad1.py", "from test_other import helper\n"),
        ("infra/test_bad2.py", "import tests.core.test_models\n"),
        ("persona/test_bad3.py", "from . import test_presets\n"),
        ("architecture/test_bad4.py", "from ..core.test_models import helper\n"),
        ("app/test_bad5.py", "from tests import test_root\n"),
        ("support/bad_support.py", "from tests.app import test_routes\n"),
    ],
)
def test_mock_test_isolation_catches_cross_test_imports(tmp_path, rel_path, content):
    _, tests_root = _build_valid_mock_repo(tmp_path)
    _write_tree(tests_root, {rel_path: content})
    violations = check_test_isolation(tests_root)
    assert any(rel_path in msg for msg in violations)


def test_mock_test_isolation_checks_full_tree_once_migrated(tmp_path):
    _, tests_root = _build_valid_mock_repo(tmp_path)
    _write_tree(
        tests_root,
        {
            "custom_migrated/test_custom.py": "from test_legacy import helper\n",
        },
    )
    violations = check_test_isolation(tests_root)
    assert any("custom_migrated/test_custom.py" in msg for msg in violations)


def test_mock_test_isolation_respects_transitional_root_while_guarding_migrated_dirs(tmp_path):
    _, tests_root = _build_valid_mock_repo(tmp_path)
    _write_tree(
        tests_root,
        {
            "test_legacy_a.py": "from test_legacy_b import helper\n",
            "test_legacy_b.py": "helper = 1\n",
        },
    )
    assert check_test_isolation(tests_root) == []

    _write_tree(tests_root, {"core/test_bad_migrated.py": "from test_legacy_b import helper\n"})
    violations = check_test_isolation(tests_root)
    assert any("core/test_bad_migrated.py" in msg for msg in violations)
