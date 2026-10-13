"""Hooks service for OpenHands Agent Server.

This module contains the business logic for loading hooks from the workspace,
keeping the router clean and focused on HTTP concerns.

Hook Sources:
- Project hooks: {workspace}/.openhands/hooks.json
- User hooks: ~/.openhands/hooks.json (or $OH_PERSISTENCE_DIR/hooks.json)
"""

from pathlib import Path

from openhands.sdk.hooks import HookConfig
from openhands.sdk.logger import get_logger
from openhands.sdk.utils.path import get_user_persistence_dir


logger = get_logger(__name__)


def load_hooks(
    project_dir: str | None = None,
    load_project: bool = True,
    load_user: bool = True,
) -> HookConfig | None:
    """Load project and user hooks, merged.

    Project hooks come first, so their handlers run before user handlers for
    the same event. A project dir whose ``.openhands/hooks.json`` is the user
    hooks file is read once.

    Args:
        project_dir: Workspace directory path for project hooks.
        load_project: Load ``{project_dir}/.openhands/hooks.json``.
        load_user: Load the user hooks file, ``~/.openhands/hooks.json``.

    Returns:
        The merged HookConfig, or None if no source has hooks.
    """
    paths: list[Path] = []
    if load_project:
        if project_dir:
            paths.append(Path(project_dir) / ".openhands" / "hooks.json")
        else:
            logger.debug("No project_dir provided, skipping project hooks")
    if load_user:
        user_path = get_user_persistence_dir() / "hooks.json"
        if not any(_same_file(user_path, p) for p in paths):
            paths.append(user_path)

    configs = [c for c in (_load_hooks_file(p) for p in paths) if c is not None]
    return HookConfig.merge(configs)


def load_hooks_from_workspace(project_dir: str | None = None) -> HookConfig | None:
    """Load hooks from the workspace .openhands/hooks.json file only.

    Args:
        project_dir: Workspace directory path for project hooks.

    Returns:
        HookConfig if hooks.json exists and is valid, None otherwise.
    """
    return load_hooks(project_dir=project_dir, load_project=True, load_user=False)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _load_hooks_file(hooks_path: Path) -> HookConfig | None:
    if not hooks_path.exists():
        logger.debug(f"No hooks.json found at {hooks_path}")
        return None

    try:
        hook_config = HookConfig.load(path=hooks_path)

        if hook_config.is_empty():
            logger.debug(f"hooks.json at {hooks_path} is empty")
            return None

        logger.info(f"Loaded hooks from {hooks_path}")
        return hook_config

    except Exception as e:
        logger.warning(f"Failed to load hooks from {hooks_path}: {e}")
        return None
