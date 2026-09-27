from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat


def ensure_private_directory(path: Path) -> Path:
    """Return a real managed directory, detaching a legacy symlink if needed."""
    target = Path(path).expanduser()
    if target.is_symlink():
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    return target


def ensure_private_inheritance_directory(path: Path, source: Path) -> Path:
    """Create a private target and reject a source/target directory alias."""
    target = ensure_private_directory(path)
    source_path = Path(source).expanduser()
    if Path(os.path.abspath(source_path)) == Path(os.path.abspath(target)):
        raise ValueError(f'inheritance target must differ from source: {target}')
    try:
        if source_path.exists() and os.path.samefile(source_path, target):
            raise ValueError(f'inheritance target aliases source: {target}')
    except FileNotFoundError:
        pass
    return target


def ensure_private_descendant_directory(root: Path, relative: Path) -> Path:
    """Create a real directory below a managed root without following links.

    Each descendant component is detached separately. This matters for legacy
    homes that may contain a link such as ``.config -> ~/.config``: creating
    only the final directory would otherwise follow that link and let the
    managed provider write back into the user's external state.
    """
    managed_root = ensure_private_directory(root)
    relative_path = Path(relative)
    if relative_path.is_absolute() or '..' in relative_path.parts:
        raise ValueError(f'private descendant must be relative to {managed_root}: {relative_path}')
    current = managed_root
    for component in relative_path.parts:
        if component in {'', '.'}:
            continue
        current = current / component
        ensure_private_directory(current)
    return current


def copy_regular_file(source: Path, target: Path) -> bool:
    """Copy one regular file source -> target without following either symlink."""
    src = Path(source).expanduser()
    dst = Path(target).expanduser()
    # Do not let an explicit profile turn the inheritance source into its own
    # writable target. Compare lexical paths before touching the destination;
    # resolving here would incorrectly reject a destination symlink that must
    # instead be detached.
    if Path(os.path.abspath(src)) == Path(os.path.abspath(dst)):
        return False
    _detach_destination_file_alias(dst)
    if not src.is_file() or src.is_symlink():
        return False
    ensure_private_directory(dst.parent)
    if dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            return False
        dst.unlink()
    shutil.copy2(src, dst, follow_symlinks=False)
    return True


def copy_regular_tree(source: Path, target: Path) -> int:
    """Merge regular files source -> target while ignoring all source symlinks."""
    src = Path(source).expanduser()
    dst = Path(target).expanduser()
    if not src.is_dir() or src.is_symlink():
        return 0
    ensure_private_directory(dst)
    copied = 0
    for source_path in src.rglob('*'):
        if source_path.is_symlink() or not source_path.is_file():
            continue
        relative = source_path.relative_to(src)
        try:
            _ensure_descendant_directory(dst, relative.parent)
            copied += int(copy_regular_file(source_path, dst / relative))
        except OSError:
            continue
    return copied


def _detach_destination_file_alias(path: Path) -> bool:
    """Remove a managed file alias while preserving the external inode."""
    target = Path(path).expanduser()
    try:
        if target.is_symlink():
            target.unlink()
            return True
        metadata = target.stat(follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            target.unlink()
            return True
    except FileNotFoundError:
        return False
    return False


def _ensure_descendant_directory(root: Path, relative: Path) -> None:
    ensure_private_descendant_directory(root, relative)


__all__ = [
    'copy_regular_file',
    'copy_regular_tree',
    'ensure_private_descendant_directory',
    'ensure_private_directory',
    'ensure_private_inheritance_directory',
]
