from __future__ import annotations

from pathlib import Path
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from release_artifacts import release_artifact_name
from platforms.windows.release.surface import load_windows_x64_release_surface_projection
from cli.roles_runtime.commands import cmd_roles
from cli.services.mobile_host import (
    restart_running_mobile_host_service,
    start_or_replace_mobile_host_service,
)
from cli.services.mobile_update import (
    DEFAULT_MOBILE_GATEWAY_LISTEN,
    run_mobile_cloudflare_onboarding,
    run_mobile_lan_onboarding,
    run_mobile_relay_onboarding,
    run_mobile_update_onboarding,
    suggest_lan_listen_address,
)
from cli.services.mobile_route_onboarding import (
    MobileRouteSelection,
    ensure_guided_relay_credentials,
    prompt_mobile_route_selection,
)
from cli.tools_runtime.workbench import print_workbench_status, update_rich_workbench
from rolepacks.sources import role_catalog_status

from ..install import (
    download_tarball,
    is_source_repo_root,
    npm_install_provenance,
    npm_update_command,
    pick_temp_base_dir,
    resolve_managed_install_dir,
    run_staged_unix_installer,
    safe_extract_tar,
)
from ..provider_cache_cleanup import run_post_update_provider_cache_cleanup
from ..provider_updates import run_provider_update_flow
from ..versioning import REPO_URL, format_version_info, get_available_versions, get_version_info
from .matching import find_matching_version, latest_version


POST_UPDATE_COMMAND = "__post-update"
POST_UPDATE_TIMEOUT_SECONDS = 300.0
POST_UPDATE_WITH_PROVIDERS_TIMEOUT_SECONDS = 60.0 * 60.0
ENTRYPOINT_SMOKE_TIMEOUT_SECONDS = 30.0
DEFAULT_CATALOG_ROLE_IDS = ('agentroles.archi', 'agentroles.ccb_self')


def set_tmux_ui_active(active: bool) -> None:
    from cli.services.tmux_ui import set_tmux_ui_active as _set_tmux_ui_active

    _set_tmux_ui_active(active)


def cmd_update(args, *, script_root: Path) -> int:
    if _update_target_is_rich(args):
        return _update_rich_bundle()
    if _update_target_is_mobile(args):
        return _update_mobile_bundle(script_root=script_root, args=args)
    if platform.system() == "Windows":
        return _cmd_update_windows_release_surface(args, script_root=script_root)
    supported, reason = _supported_update_platform()
    if not supported:
        print(reason)
        return 1
    source_repo_install = is_source_repo_root(script_root)
    install_dir = resolve_managed_install_dir(script_root=script_root)

    target_version = _resolve_target_version(args)
    if target_version is False:
        return 1
    npm_provenance = npm_install_provenance(script_root=script_root)
    if npm_provenance is not None:
        command = npm_update_command(target_version if isinstance(target_version, str) else None)
        print("ℹ️  This CCB installation is managed by npm; its vendored release cannot update itself in place.")
        print(f"   Run: {command}")
        return 0

    current_install_root = script_root if source_repo_install else install_dir
    old_info = get_version_info(current_install_root)
    provider_mode = _provider_update_mode(args)
    cache_cleanup_enabled = _cache_cleanup_enabled(args)
    if target_version:
        if source_repo_install:
            print(f"🔄 Installing release v{target_version} from source/dev checkout...")
        else:
            print(f"🔄 Updating to v{target_version}...")
    else:
        if source_repo_install:
            print("🔄 Checking latest stable release for source/dev checkout...")
        else:
            print("🔄 Checking for release updates...")

    resolved_target = target_version or _resolve_latest_release_version()
    if not resolved_target:
        print("❌ Could not determine latest release version")
        return 1
    if (
        not source_repo_install
        and not target_version
        and _identity_value(old_info, "version") == resolved_target
    ):
        _print_update_outcome(old_info, old_info)
        _run_provider_updates_nonblocking(mode=provider_mode)
        return 0
    try:
        tmp_base = pick_temp_base_dir(install_dir)
    except Exception as exc:
        print(str(exc))
        return 1
    code = _update_via_tarball(
        tmp_base,
        install_dir=install_dir,
        target_version=resolved_target,
        old_info=old_info,
        provider_mode=provider_mode,
        cache_cleanup_enabled=cache_cleanup_enabled,
    )
    if code != 0:
        return code
    if source_repo_install:
        print(f"ℹ️  Global `ccb` links now target the release install at: {install_dir}")
        print("   `./ccb` inside the source checkout still runs the live source tree.")
    return 0


def _resolve_target_version(args) -> str | bool | None:
    if not hasattr(args, "target") or not args.target:
        return None
    target_spec = args.target.lstrip("v")
    if not re.match(r"^\d+(\.\d+)*$", target_spec):
        print(f"❌ Invalid version format: {args.target}")
        print("   Examples: ccb update 4, ccb update 4.1, ccb update 4.1.3")
        return False
    print(f"🔍 Looking for version matching: {target_spec}")
    versions = get_available_versions()
    if not versions:
        print("❌ Could not fetch available versions")
        return False
    target_version = find_matching_version(target_spec, versions)
    if not target_version:
        ordered = sorted(versions, key=lambda item: [int(x) for x in item.split(".")], reverse=True)[:10]
        print(f"❌ No version found matching '{target_spec}'")
        print(f"   Available: {', '.join(ordered)}")
        return False
    print(f"📌 Target version: v{target_version}")
    return target_version


def _supported_update_platform() -> tuple[bool, str | None]:
    system_name = platform.system()
    if system_name in {"Linux", "Darwin"}:
        return True, None
    return (
        False,
        "❌ `ccb update` is currently supported only on Linux/macOS/WSL.\n"
        "   Please use a Linux, macOS, or WSL runtime, or reinstall manually on this platform.",
    )


def _cmd_update_windows_release_surface(args, *, script_root: Path) -> int:
    projection = load_windows_x64_release_surface_projection(
        script_root,
        _windows_update_host_evidence(),
    )
    update_entry = str(projection.get("update_entry") or "diagnostic_only")
    if update_entry in {"diagnostic_only", "npm", "source"}:
        _print_windows_release_surface_update_diagnostic(projection, update_entry=update_entry)
        return 1
    if update_entry != "install_ps1":
        _print_windows_release_surface_update_diagnostic(
            projection,
            update_entry="diagnostic_only",
            fallback="Windows update projection has an unsupported update_entry.",
        )
        return 1

    target_version = _resolve_target_version(args)
    if target_version is False:
        return 1
    install_dir = resolve_managed_install_dir(script_root=script_root)
    old_info = get_version_info(install_dir)
    provider_mode = _provider_update_mode(args)
    cache_cleanup_enabled = _cache_cleanup_enabled(args)
    resolved_target = target_version or _resolve_latest_release_version()
    if not resolved_target:
        print("❌ Could not determine latest release version")
        return 1
    try:
        tmp_base = pick_temp_base_dir(install_dir)
    except Exception as exc:
        print(str(exc))
        return 1
    return _update_via_windows_release_surface(
        tmp_base,
        install_dir=install_dir,
        target_version=resolved_target,
        old_info=old_info,
        projection=projection,
        provider_mode=provider_mode,
        cache_cleanup_enabled=cache_cleanup_enabled,
    )


def _windows_update_host_evidence() -> dict[str, object]:
    system_name = platform.system()
    machine = platform.machine()
    env_arch = str(os.environ.get("PROCESSOR_ARCHITECTURE") or machine or "").strip()
    native_arch = str(os.environ.get("PROCESSOR_ARCHITEW6432") or "").strip()
    return {
        "os_platform": _release_surface_os_platform(system_name),
        "cpu_arch": _release_surface_cpu_arch(native_arch or env_arch or machine),
        "process_arch": _release_surface_cpu_arch(env_arch or machine),
        "wow64": bool(native_arch and _release_surface_cpu_arch(env_arch) == "ia32"),
        "installer_entrypoint": "update",
    }


def _release_surface_os_platform(system_name: str) -> str:
    if system_name == "Windows":
        return "win32"
    if system_name == "Darwin":
        return "darwin"
    if system_name == "Linux":
        return "linux"
    return "unknown"


def _release_surface_cpu_arch(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"amd64", "x86_64", "x64"}:
        return "x64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    if normalized in {"x86", "i386", "i686", "ia32"}:
        return "ia32"
    return "unknown"


def _print_windows_release_surface_update_diagnostic(
    projection: dict[str, object],
    *,
    update_entry: str,
    fallback: str | None = None,
) -> None:
    diagnostic = str(projection.get("diagnostic") or fallback or "Windows x64 update route is not available.")
    next_action = str(projection.get("next_action") or "").strip()
    failure_reason = str(projection.get("failure_reason") or "unknown")
    print(f"❌ Windows x64 update route is {update_entry}: {diagnostic}")
    print(f"   failure_reason={failure_reason}")
    if next_action:
        print(f"   next_action={next_action}")


def _resolve_latest_release_version() -> str | None:
    versions = get_available_versions()
    return latest_version(versions)


def _update_via_tarball(
    tmp_base: Path,
    *,
    install_dir: Path,
    target_version: str | None,
    old_info: dict[str, object],
    provider_mode: str = "prompt",
    cache_cleanup_enabled: bool = True,
) -> int:
    if not target_version:
        print("❌ Update failed: no release version selected")
        return 1
    artifact_name = _release_artifact_name()
    if not artifact_name:
        print(
            "❌ Update failed: unsupported release artifact target "
            f"for platform '{platform.system()}' architecture '{platform.machine()}'"
        )
        return 1
    tarball_url = _release_artifact_url(target_version, artifact_name=artifact_name)
    extracted_name = artifact_name

    transaction_dir: Path | None = None
    backup_dir: Path | None = None
    preserve_backup = False
    install_attempted = False
    try:
        transaction_dir = _safe_update_transaction_dir(tmp_base=tmp_base, install_dir=install_dir)
        print(f"📥 Downloading v{target_version}...")
        tarball_path = transaction_dir / artifact_name
        if not download_tarball(tarball_url, tarball_path):
            print("❌ Update failed: unable to download release tarball")
            return 1

        print("📂 Extracting...")
        with tarfile.open(tarball_path, "r:gz") as tar:
            safe_extract_tar(tar, transaction_dir)
        extracted_dir = transaction_dir / _release_extract_dir_name(extracted_name)
        staged_info = get_version_info(extracted_dir)
        identity_error = _update_identity_error(
            old_info=old_info,
            staged_info=staged_info,
            target_version=target_version,
        )
        if identity_error:
            print(f"❌ Update failed: {identity_error}")
            return 1

        backup_dir = _backup_install_prefix(install_dir=install_dir, transaction_dir=transaction_dir)

        print("🔧 Installing...")
        install_attempted = True
        returncode = run_staged_unix_installer(
            "install",
            source_dir=extracted_dir,
            install_dir=install_dir,
            extra_env={
                "CODEX_INSTALL_PREFIX": str(install_dir),
                "CCB_CLEAN_INSTALL": "1",
                "CCB_INSTALL_ROLES": "0",
            },
        )
        if returncode != 0:
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
            print(f"❌ Update failed: installer exited with code {returncode}")
            return returncode

        new_info = get_version_info(install_dir)
        installed_identity_error = _installed_identity_error(staged_info=staged_info, new_info=new_info)
        if installed_identity_error:
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
            print(f"❌ Update failed: {installed_identity_error}")
            return 1
        _print_update_outcome(old_info, new_info)
        if not _run_post_update_with_new_entrypoint(
            install_dir=install_dir,
            old_info=old_info,
            new_info=new_info,
            provider_mode=provider_mode,
            cache_cleanup_enabled=cache_cleanup_enabled,
        ):
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
            return 1
        return 0
    except Exception as exc:
        if install_attempted:
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
        print(f"❌ Update failed: {exc}")
        return 1
    finally:
        if transaction_dir is not None and not preserve_backup:
            shutil.rmtree(transaction_dir, ignore_errors=True)


def _update_via_windows_release_surface(
    tmp_base: Path,
    *,
    install_dir: Path,
    target_version: str | None,
    old_info: dict[str, object],
    projection: dict[str, object],
    provider_mode: str = "prompt",
    cache_cleanup_enabled: bool = True,
) -> int:
    if not target_version:
        print("❌ Update failed: no release version selected")
        return 1
    archive_name = _required_projection_text(projection, "archive_name")
    extract_dir_name = _required_projection_text(projection, "extract_dir")
    checksum_entry = _required_projection_text(projection, "checksum_entry")
    release_artifact_ref = _required_projection_text(projection, "release_artifact_ref")
    installer_entry = _required_projection_text(projection, "windows_installer_entry")
    if not archive_name or not extract_dir_name or not checksum_entry or not release_artifact_ref or installer_entry != "install.ps1":
        print("❌ Update failed: Windows release projection is missing a valid install.ps1 route")
        return 1

    tarball_url = _release_artifact_url(target_version, artifact_name=archive_name)
    checksum_url = _release_artifact_url(target_version, artifact_name="SHA256SUMS")
    transaction_dir: Path | None = None
    backup_dir: Path | None = None
    preserve_backup = False
    install_attempted = False
    try:
        transaction_dir = _safe_update_transaction_dir(tmp_base=tmp_base, install_dir=install_dir)
        print(f"📥 Downloading v{target_version}...")
        archive_path = transaction_dir / archive_name
        if not download_tarball(tarball_url, archive_path):
            print("❌ Update failed: unable to download Windows release archive")
            return 1
        sums_path = transaction_dir / "SHA256SUMS"
        if not download_tarball(checksum_url, sums_path):
            print("❌ Update failed: unable to download Windows release checksums")
            return 1
        checksum_error = _windows_release_checksum_error(
            archive_path=archive_path,
            sums_path=sums_path,
            checksum_entry=checksum_entry,
        )
        if checksum_error:
            print(f"❌ Update failed: {checksum_error}")
            return 1

        print("📂 Extracting...")
        _extract_windows_release_archive(archive_path, transaction_dir)
        extracted_dir = transaction_dir / extract_dir_name
        installer_path = extracted_dir / installer_entry
        if not installer_path.exists():
            print("❌ Update failed: staged Windows installer entry is missing")
            return 1

        staged_info = get_version_info(extracted_dir)
        identity_error = _update_identity_error(
            old_info=old_info,
            staged_info=staged_info,
            target_version=target_version,
        )
        if identity_error:
            print(f"❌ Update failed: {identity_error}")
            return 1

        backup_dir = _backup_install_prefix(install_dir=install_dir, transaction_dir=transaction_dir)

        print("🔧 Installing...")
        install_attempted = True
        returncode = _run_staged_windows_installer(
            "install",
            source_dir=extracted_dir,
            install_dir=install_dir,
            installer_entry=installer_entry,
            extra_env={
                "CODEX_INSTALL_PREFIX": str(install_dir),
                "CCB_CLEAN_INSTALL": "1",
                "CCB_INSTALL_ROLES": "0",
            },
        )
        if returncode != 0:
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
            print(f"❌ Update failed: installer exited with code {returncode}")
            return returncode

        new_info = get_version_info(install_dir)
        installed_identity_error = _installed_identity_error(staged_info=staged_info, new_info=new_info)
        if installed_identity_error:
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
            print(f"❌ Update failed: {installed_identity_error}")
            return 1
        _print_update_outcome(old_info, new_info)
        if not _run_post_update_with_new_entrypoint(
            install_dir=install_dir,
            old_info=old_info,
            new_info=new_info,
            provider_mode=provider_mode,
            cache_cleanup_enabled=cache_cleanup_enabled,
        ):
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
            return 1
        return 0
    except Exception as exc:
        if install_attempted:
            preserve_backup = _restore_or_retain_backup(install_dir=install_dir, backup_dir=backup_dir)
        print(f"❌ Update failed: {exc}")
        return 1
    finally:
        if transaction_dir is not None and not preserve_backup:
            shutil.rmtree(transaction_dir, ignore_errors=True)


def _required_projection_text(projection: dict[str, object], field: str) -> str | None:
    value = projection.get(field)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _windows_release_checksum_error(
    *,
    archive_path: Path,
    sums_path: Path,
    checksum_entry: str,
) -> str | None:
    checksums = _parse_sha256_sums(sums_path.read_text(encoding="utf-8", errors="ignore"))
    expected = checksums.get(Path(checksum_entry).name)
    if not expected:
        return f"SHA256SUMS does not contain {checksum_entry}"
    actual = _sha256_file(archive_path)
    if actual != expected:
        return f"checksum mismatch for {archive_path.name}"
    return None


def _parse_sha256_sums(text: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^([a-fA-F0-9]{64})\s+\*?(.+)$", stripped)
        if match:
            checksums[Path(match.group(2)).name] = match.group(1).lower()
    return checksums


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_windows_release_archive(archive_path: Path, destination: Path) -> None:
    if archive_path.suffix.lower() == ".zip":
        _safe_extract_zip(archive_path, destination)
        return
    with tarfile.open(archive_path, "r:gz") as tar:
        safe_extract_tar(tar, destination)


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if not _path_is_within(destination, member_path):
                raise RuntimeError(f"Unsafe zip member path: {member.filename}")
        archive.extractall(destination)


def _run_staged_windows_installer(
    action: str,
    *,
    source_dir: Path,
    install_dir: Path,
    installer_entry: str = "install.ps1",
    extra_env: dict[str, str] | None = None,
) -> int:
    script = Path(source_dir).expanduser() / installer_entry
    if not script.exists():
        print(f"❌ install.ps1 not found in {source_dir}", file=sys.stderr)
        return 1
    powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    env = os.environ.copy()
    env["CODEX_INSTALL_PREFIX"] = str(install_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            action,
            "-InstallPrefix",
            str(install_dir),
        ],
        cwd=source_dir,
        env=env,
    ).returncode


def _update_identity_error(
    *,
    old_info: dict[str, object],
    staged_info: dict[str, object],
    target_version: str,
) -> str | None:
    staged_version = _identity_value(staged_info, "version")
    if staged_version != target_version:
        return f"release artifact version mismatch (requested v{target_version}, artifact v{staged_version or 'unknown'})"
    old_version = _identity_value(old_info, "version")
    if old_version != staged_version:
        return None
    old_commit = _identity_value(old_info, "commit")
    staged_commit = _identity_value(staged_info, "commit")
    if old_commit and staged_commit and old_commit == staged_commit:
        return None
    return "same-version build identity collision; use a uniquely versioned release artifact"


def _installed_identity_error(*, staged_info: dict[str, object], new_info: dict[str, object]) -> str | None:
    for field in ("version", "commit"):
        staged_value = _identity_value(staged_info, field)
        installed_value = _identity_value(new_info, field)
        if not staged_value or staged_value != installed_value:
            return f"installed build identity does not match the release artifact ({field})"
    return None


def _identity_value(info: dict[str, object], field: str) -> str | None:
    value = str(info.get(field) or "").strip()
    return value or None


def _backup_install_prefix(*, install_dir: Path, transaction_dir: Path) -> Path | None:
    if not install_dir.exists():
        return None
    backup_dir = transaction_dir / "previous-install"
    shutil.copytree(install_dir, backup_dir, symlinks=True)
    return backup_dir


def _safe_update_transaction_dir(*, tmp_base: Path, install_dir: Path) -> Path:
    install_dir = Path(install_dir).expanduser()
    if install_dir.is_symlink():
        raise RuntimeError("refusing update because the install prefix is a symbolic link")
    install_root = _resolved_update_path(install_dir)
    candidate_roots = (Path(tmp_base).expanduser(), Path(tempfile.gettempdir()).expanduser())
    seen: set[Path] = set()
    for candidate_root in candidate_roots:
        resolved_root = _resolved_update_path(candidate_root)
        if resolved_root in seen or _path_is_within(install_root, resolved_root):
            continue
        seen.add(resolved_root)
        try:
            resolved_root.mkdir(parents=True, exist_ok=True)
            transaction_dir = Path(tempfile.mkdtemp(prefix="ccb-update-", dir=str(resolved_root)))
        except OSError:
            continue
        if _path_is_within(install_root, _resolved_update_path(transaction_dir)):
            shutil.rmtree(transaction_dir, ignore_errors=True)
            continue
        return transaction_dir
    raise RuntimeError("no safe external rollback storage is available for this update")


def _resolved_update_path(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _path_is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _restore_install_prefix(*, install_dir: Path, backup_dir: Path | None) -> None:
    if install_dir.exists() or install_dir.is_symlink():
        if install_dir.is_dir() and not install_dir.is_symlink():
            shutil.rmtree(install_dir)
        else:
            install_dir.unlink()
    if backup_dir is None:
        return
    shutil.copytree(backup_dir, install_dir, symlinks=True)


def _restore_or_retain_backup(*, install_dir: Path, backup_dir: Path | None) -> bool:
    try:
        _restore_install_prefix(install_dir=install_dir, backup_dir=backup_dir)
        return False
    except Exception as exc:
        if backup_dir is not None and backup_dir.exists():
            print(f"❌ Update rollback failed: {exc}; recoverable backup retained at: {backup_dir}")
            return True
        print(f"❌ Update rollback cleanup failed: {exc}")
        return False


def maybe_handle_post_update_command(tokens: list[str], *, script_root: Path) -> int | None:
    if list(tokens[:1]) != [POST_UPDATE_COMMAND]:
        return None
    from_version = _post_update_option(tokens, '--from-version', default='unknown')
    to_version = _post_update_option(tokens, '--to-version', default='unknown')
    cache_cleanup_enabled = (
        '--no-cache-cleanup' not in tokens
        and not _falsey_env('CCB_POST_UPDATE_CACHE_CLEANUP_ENABLED')
    )
    return _run_post_update_provisioning(
        install_dir=Path(script_root).expanduser(),
        from_version=from_version,
        to_version=to_version,
        cache_cleanup_enabled=cache_cleanup_enabled,
    )


def _run_post_update_with_new_entrypoint(
    *,
    install_dir: Path,
    old_info: dict[str, object],
    new_info: dict[str, object],
    provider_mode: str = "prompt",
    cache_cleanup_enabled: bool = True,
) -> bool:
    ccb_entry = _installed_ccb_entrypoint(install_dir)
    if not _verify_installed_ccb_entrypoint(ccb_entry):
        return False
    command = [
        str(ccb_entry),
        POST_UPDATE_COMMAND,
        "--from-version",
        _post_update_version_label(old_info),
        "--to-version",
        _post_update_version_label(new_info),
    ]
    env = dict(os.environ)
    env["CODEX_INSTALL_PREFIX"] = str(install_dir)
    env["CCB_SKIP_STARTUP_UPDATE_CHECK"] = "1"
    env["CCB_PROVIDER_UPDATE_FLOW"] = "1"
    env["CCB_PROVIDER_UPDATE_MODE"] = _normalized_provider_update_mode(provider_mode)
    env['CCB_POST_UPDATE_CACHE_CLEANUP_FLOW'] = '1'
    env['CCB_POST_UPDATE_CACHE_CLEANUP_ENABLED'] = '1' if cache_cleanup_enabled else '0'
    env['CCB_POST_UPDATE_MOBILE_HOST_REFRESH_FLOW'] = '1'
    if not cache_cleanup_enabled:
        command.append('--no-cache-cleanup')
    timeout = _post_update_timeout_seconds(
        provider_flow=_normalized_provider_update_mode(provider_mode) != "none",
        cache_cleanup=cache_cleanup_enabled,
    )
    try:
        result = subprocess.run(command, cwd=Path.cwd(), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        if _post_update_failure_is_required():
            print(f"❌ Required post-update provisioning timed out after {timeout:g}s.")
            return False
        print(f"⚠️  Post-update provisioning timed out after {timeout:g}s.")
        print(
            "   Core update completed; retry Role Pack checks with `ccb roles list` "
            "and provider checks with `ccb update --providers check`."
        )
        return True
    except Exception as exc:
        if _post_update_failure_is_required():
            print(f"❌ Required post-update provisioning failed to run: {type(exc).__name__}: {exc}")
            return False
        print(f"⚠️  Post-update provisioning skipped: {type(exc).__name__}: {exc}")
        return True
    if result.returncode != 0:
        if _post_update_failure_is_required():
            print(f"❌ Required post-update provisioning exited with code {result.returncode}.")
            return False
        print(f"⚠️  Post-update provisioning exited with code {result.returncode}.")
        print(
            "   Core update completed; retry Role Pack checks with `ccb roles list` "
            "and provider checks with `ccb update --providers check`."
        )
    return True


def _installed_ccb_entrypoint(install_dir: Path) -> Path:
    bin_dir = str(os.environ.get("CODEX_BIN_DIR") or "").strip()
    if bin_dir:
        return Path(bin_dir).expanduser() / "ccb"
    install_root = Path(install_dir).expanduser()
    candidates: list[Path] = []
    argv0 = str(sys.argv[0] if sys.argv else "").strip()
    if argv0:
        current_entry = Path(argv0).expanduser()
        if current_entry.name == "ccb":
            candidates.append(current_entry)
    candidates.append(Path.home() / ".local" / "bin" / "ccb")
    candidates.append(install_root / "ccb")
    for candidate in candidates:
        if _entrypoint_targets_install_dir(candidate, install_root):
            return candidate
    return install_root / "ccb"


def _entrypoint_targets_install_dir(candidate: Path, install_dir: Path) -> bool:
    try:
        resolved_candidate = Path(candidate).expanduser().resolve()
        installed_entry = Path(install_dir).expanduser().resolve() / "ccb"
        if resolved_candidate == installed_entry:
            return True
    except Exception:
        installed_entry = Path(install_dir).expanduser() / "ccb"
    try:
        text = Path(candidate).expanduser().read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return str(installed_entry) in text or str(Path(install_dir).expanduser() / "ccb") in text


def _verify_installed_ccb_entrypoint(ccb_entry: Path) -> bool:
    if not ccb_entry.exists():
        print(f"❌ Update failed: installed ccb entrypoint not found: {ccb_entry}")
        return False
    try:
        result = subprocess.run(
            [str(ccb_entry), "--print-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_entrypoint_smoke_timeout_seconds(),
        )
    except subprocess.TimeoutExpired:
        print(f"❌ Update failed: installed ccb entrypoint smoke check timed out after {_entrypoint_smoke_timeout_seconds():g}s")
        return False
    except Exception as exc:
        print(f"❌ Update failed: installed ccb entrypoint smoke check could not run: {type(exc).__name__}: {exc}")
        return False
    if result.returncode == 0:
        return True
    print(f"❌ Update failed: installed ccb entrypoint failed runtime smoke check: {ccb_entry}")
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        print(f"   {detail.splitlines()[0]}")
    return False


def _post_update_version_label(info: dict[str, object]) -> str:
    value = info.get("version") or info.get("commit") or "unknown"
    return str(value)


def _post_update_timeout_seconds(
    *,
    provider_flow: bool = False,
    cache_cleanup: bool = False,
) -> float:
    default = (
        POST_UPDATE_WITH_PROVIDERS_TIMEOUT_SECONDS
        if provider_flow or cache_cleanup
        else POST_UPDATE_TIMEOUT_SECONDS
    )
    return _positive_float_env("CCB_POST_UPDATE_TIMEOUT_SECONDS", default)


def _entrypoint_smoke_timeout_seconds() -> float:
    return _positive_float_env("CCB_ENTRYPOINT_SMOKE_TIMEOUT_SECONDS", ENTRYPOINT_SMOKE_TIMEOUT_SECONDS)


def _positive_float_env(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _post_update_failure_is_required() -> bool:
    # In post-update context, force env vars mean both "do not prompt" and
    # "treat provisioning failure as required".
    return (
        _truthy_env("CCB_POST_UPDATE_REQUIRED")
        or _truthy_env("CCB_INSTALL_ROLES")
    )


def _truthy_env(name: str) -> bool:
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "on", "yes"}


def _falsey_env(name: str) -> bool:
    value = str(os.environ.get(name) or '').strip().lower()
    return value in {'0', 'false', 'off', 'no'}


def _run_post_update_provisioning(
    *,
    install_dir: Path,
    from_version: str = 'unknown',
    to_version: str = 'unknown',
    cache_cleanup_enabled: bool = True,
) -> int:
    failures = 0
    try:
        set_tmux_ui_active(True)
    except Exception as exc:
        print(f"⚠️  Tmux UI post-update refresh skipped: {type(exc).__name__}: {exc}")
    try:
        failures += int(_update_builtin_roles_after_update(install_dir=install_dir) or 0)
    except Exception as exc:
        failures += 1
        print(f"⚠️  Role Pack post-update provisioning failed: {type(exc).__name__}: {exc}")
    if _truthy_env("CCB_PROVIDER_UPDATE_FLOW"):
        _run_provider_updates_nonblocking(
            mode=os.environ.get("CCB_PROVIDER_UPDATE_MODE") or "prompt",
        )
    if (
        cache_cleanup_enabled
        and _truthy_env('CCB_POST_UPDATE_CACHE_CLEANUP_FLOW')
        and not (failures and _post_update_failure_is_required())
    ):
        try:
            run_post_update_provider_cache_cleanup(
                from_version=from_version,
                to_version=to_version,
                cwd=Path.cwd(),
            )
        except Exception as exc:
            print(
                '⚠️  Post-update legacy cache migration failed; '
                f'the core update is unaffected: {type(exc).__name__}: {exc}'
            )
    if (
        _truthy_env('CCB_POST_UPDATE_MOBILE_HOST_REFRESH_FLOW')
        and not (failures and _post_update_failure_is_required())
    ):
        try:
            refreshed_host = restart_running_mobile_host_service(
                script_root=install_dir,
            )
        except Exception as exc:
            print(
                '⚠️  Mobile Host post-update refresh failed; '
                f'the core update is unaffected: {type(exc).__name__}: {exc}'
            )
            print('   Run `ccb update mobile` to restart it with the installed version.')
        else:
            if refreshed_host is not None:
                print(
                    '✅ Mobile Host refreshed with the installed CCB version: '
                    f'pid={refreshed_host.pid} route={refreshed_host.route_provider}'
                )
    return 1 if failures else 0


def _print_update_outcome(old_info: dict[str, object], new_info: dict[str, object]) -> None:
    old_str = format_version_info(old_info)
    new_str = format_version_info(new_info)
    if old_info.get("commit") != new_info.get("commit") or old_info.get("version") != new_info.get("version"):
        print(f"✅ Updated: {old_str} → {new_str}")
    else:
        print(f"✅ Already up to date: {new_str}")


def _update_builtin_roles_after_update(*, install_dir: Path) -> int:
    return _update_catalog_roles_after_update(install_dir=install_dir)


def _update_catalog_roles_after_update(*, install_dir: Path) -> int:
    choice = _roles_update_choice()
    if choice == 'env-skip':
        print('ℹ️  Role Pack update skipped by CCB_INSTALL_ROLES=0')
        return 0
    try:
        rows = tuple(role_catalog_status(refresh_default=True))
    except Exception as exc:
        print(f'⚠️  Agent Roles catalog unavailable: {type(exc).__name__}: {exc}')
        return 1
    failures = _refresh_installed_catalog_roles(rows, install_dir=install_dir)
    refreshed_rows = _refresh_catalog_rows(rows)
    failures += _install_default_catalog_roles(refreshed_rows, install_dir=install_dir)
    refreshed_rows = _refresh_catalog_rows(refreshed_rows)
    _print_catalog_followups(refreshed_rows)
    return failures


def _refresh_catalog_rows(fallback: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    try:
        return tuple(role_catalog_status(refresh_default=True))
    except Exception:
        return fallback


def _refresh_installed_catalog_roles(rows: tuple[dict[str, object], ...], *, install_dir: Path) -> int:
    update_rows = [row for row in rows if row.get('status') == 'update_available']
    current_rows = [row for row in rows if row.get('status') == 'current']
    if not update_rows:
        if current_rows:
            print('✅ Installed Role Packs already match the catalog.')
        else:
            print('ℹ️  No installed catalog Role Packs to update.')
        return 0
    stdout = sys.stdout
    stderr = sys.stderr
    failures = 0
    for row in update_rows:
        role_id = str(row.get('role_id') or '').strip()
        if not role_id:
            continue
        code = cmd_roles(['update', role_id], script_root=install_dir, cwd=Path.cwd(), stdout=stdout, stderr=stderr)
        if code == 0:
            print(f'✅ Role Pack updated: {role_id}')
        else:
            failures += 1
            print(f'⚠️  Role Pack update failed: {role_id}')
    if failures:
        print(f'⚠️  Role Pack updates had {failures} failure(s).')
    return failures


def _install_default_catalog_roles(rows: tuple[dict[str, object], ...], *, install_dir: Path) -> int:
    default_ids = set(DEFAULT_CATALOG_ROLE_IDS)
    available = [
        row
        for row in rows
        if row.get('status') == 'available' and str(row.get('role_id') or '').strip() in default_ids
    ]
    if not available:
        return 0
    stdout = sys.stdout
    stderr = sys.stderr
    failures = 0
    for role_id in DEFAULT_CATALOG_ROLE_IDS:
        if not any(str(row.get('role_id') or '').strip() == role_id for row in available):
            continue
        code = cmd_roles(['install', role_id], script_root=install_dir, cwd=Path.cwd(), stdout=stdout, stderr=stderr)
        if code == 0:
            print(f'✅ Default Role Pack installed: {role_id}')
        else:
            failures += 1
            print(f'⚠️  Default Role Pack install failed: {role_id}')
    if failures:
        print(f'⚠️  Default Role Pack installs had {failures} failure(s).')
    return failures


def _print_catalog_followups(rows: tuple[dict[str, object], ...], *, include_default_roles: bool = False) -> None:
    default_ids = set(DEFAULT_CATALOG_ROLE_IDS)
    available_rows = [
        row
        for row in rows
        if row.get('status') == 'available'
    ]
    recommended = [
        row
        for row in available_rows
        if include_default_roles and str(row.get('role_id') or '').strip() in default_ids
    ]
    available = [row for row in available_rows if str(row.get('role_id') or '').strip() not in default_ids]
    missing = [row for row in rows if row.get('status') == 'installed_source_missing']
    if recommended:
        print('⭐ Recommended Agent Roles available:')
        _print_catalog_role_rows(recommended, include_commands=True)
        print('   Install with `ccb roles install <role-id>`; bind with `ccb roles add <role-id>:<provider>`.')
    if available:
        print('')
        print('🆕 New Agent Roles available in the catalog')
        print('   These roles were not installed automatically. Review the intro, then install the roles you want:')
        _print_catalog_role_rows(available, include_commands=True)
    for row in missing:
        role_id = str(row.get('role_id') or '').strip()
        source_path = str(row.get('path') or '').strip()
        print(f'⚠️  Installed Role Pack source missing: {role_id}' + (f' ({source_path})' if source_path else ''))


def _print_catalog_role_rows(rows: list[dict[str, object]], *, include_commands: bool = False) -> None:
    for index, row in enumerate(rows, start=1):
        role_id = str(row.get('role_id') or '').strip()
        version = str(row.get('version') or '').strip()
        name = str(row.get('name') or '').strip()
        description = _short_catalog_text(str(row.get('description') or '').strip())
        label = f'{role_id} v{version}' if version else role_id
        print(f'   {index}. {label}' + (f': {name}' if name else ''))
        if description:
            print(f'      intro: {description}')
        if include_commands and role_id:
            print(f'      install: ccb roles install {role_id}')
            print(f'      bind:    ccb roles add {role_id}:<provider>')


def _short_catalog_text(text: str, *, limit: int = 96) -> str:
    compact = ' '.join(str(text or '').split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + '...'


def _roles_update_choice() -> str:
    requested = str(os.environ.get('CCB_INSTALL_ROLES') or '').strip().lower()
    if requested in {'0', 'false', 'off', 'no'}:
        return 'env-skip'
    return 'accepted'


def _release_artifact_url(version: str, *, artifact_name: str) -> str:
    return f"{REPO_URL}/releases/download/v{version}/{artifact_name}"


def _release_artifact_name() -> str | None:
    return release_artifact_name(platform.system(), machine=platform.machine())


def _release_extract_dir_name(artifact_name: str) -> str:
    text = str(artifact_name or "").strip()
    if text.endswith(".tar.gz"):
        return text[:-7]
    if text.endswith(".tgz"):
        return text[:-4]
    return Path(text).stem


def _update_target_is_rich(args) -> bool:
    return str(getattr(args, "target", "") or "").strip().lower() == "rich"


def _update_target_is_mobile(args) -> bool:
    return str(getattr(args, "target", "") or "").strip().lower() == "mobile"


def _provider_update_mode(args) -> str:
    requested = getattr(args, "providers", None)
    if requested is None:
        requested = os.environ.get("CCB_UPDATE_PROVIDERS")
    return _normalized_provider_update_mode(requested)


def _cache_cleanup_enabled(args) -> bool:
    if _falsey_env('CCB_UPDATE_CACHE_CLEANUP'):
        return False
    return bool(getattr(args, 'cache_cleanup', True))


def _post_update_option(tokens: list[str], name: str, *, default: str) -> str:
    for index, token in enumerate(tokens):
        if token == name and index + 1 < len(tokens):
            value = str(tokens[index + 1] or '').strip()
            return value or default
        prefix = f'{name}='
        if token.startswith(prefix):
            value = token[len(prefix):].strip()
            return value or default
    return default


def _normalized_provider_update_mode(value: object) -> str:
    normalized = str(value or "prompt").strip().lower()
    return normalized if normalized in {"prompt", "check", "all", "none"} else "prompt"


def _run_provider_updates_nonblocking(*, mode: str) -> None:
    try:
        run_provider_update_flow(mode=_normalized_provider_update_mode(mode))
    except Exception as exc:
        print(f"⚠️  Provider update management skipped: {type(exc).__name__}: {exc}")


def _update_rich_bundle() -> int:
    print("🔧 Installing/updating rich workbench bundle...")
    result = update_rich_workbench()
    print_workbench_status(result)
    return 0 if result.get("status") in {"ok", "degraded"} else 1


def _update_mobile_bundle(*, script_root: Path, args) -> int:
    requested_route = str(getattr(args, 'route_provider', '') or '').strip()
    guided_selection = False
    selection = (
        MobileRouteSelection(route_provider=requested_route)
        if requested_route
        else None
    )
    if selection is None:
        if _stream_is_tty(sys.stdin) and _stream_is_tty(sys.stdout):
            selection = prompt_mobile_route_selection(
                read_fn=_read_stdio_prompt,
                print_fn=print,
            )
            if selection is None:
                return 0
            guided_selection = True
        else:
            print("ℹ️  Non-interactive mobile setup defaults to Tailscale.")
            print(
                "   Use --route-provider lan|tailnet|relay explicitly "
                "to select another route."
            )
            selection = MobileRouteSelection(route_provider='tailnet')
    requested_route = selection.route_provider

    if requested_route == 'relay':
        if guided_selection:
            credential_result = ensure_guided_relay_credentials(
                relay_mode=str(selection.relay_mode or ''),
                read_fn=_read_stdio_prompt,
                print_fn=print,
            )
            if not credential_result.ready:
                return 0 if credential_result.cancelled else 1
        listen = str(getattr(args, 'listen', '') or '').strip() or DEFAULT_MOBILE_GATEWAY_LISTEN
        public_url = str(getattr(args, 'public_url', '') or '').strip() or None

        def _start_relay_service():
            return start_or_replace_mobile_host_service(
                script_root=script_root,
                listen=listen,
                public_url=public_url,
                route_provider='relay',
                rotate_pairing=True,
            ).to_record()

        return run_mobile_relay_onboarding(start_service_fn=_start_relay_service)

    if requested_route == 'lan':
        listen = str(getattr(args, 'listen', '') or '').strip()
        suggested_listen = suggest_lan_listen_address()
        if not listen and guided_selection:
            prompt = (
                f"LAN listen address [{suggested_listen}]: "
                if suggested_listen
                else "LAN listen address (for example 192.168.1.100:8787): "
            )
            try:
                listen = (
                    _read_stdio_prompt(prompt).strip()
                    or (suggested_listen or '')
                )
            except (EOFError, KeyboardInterrupt):
                print("")
                print("Mobile setup cancelled; no gateway was changed.")
                return 0
        if not listen:
            listen = suggested_listen or ''
        if not listen:
            print("❌ Could not discover a private LAN address.")
            print(
                "   Rerun with a specific address, for example "
                "`ccb update mobile --route-provider lan "
                "--listen 192.168.1.100:8787`."
            )
            return 1
        public_url = str(getattr(args, 'public_url', '') or '').strip() or None

        def _start_lan_service():
            return start_or_replace_mobile_host_service(
                script_root=script_root,
                listen=listen,
                public_url=public_url,
                route_provider='lan',
                rotate_pairing=True,
            ).to_record()

        return run_mobile_lan_onboarding(
            start_service_fn=_start_lan_service,
            listen=listen,
        )

    if requested_route == 'cloudflare_tunnel':
        listen = str(getattr(args, 'listen', '') or '').strip() or DEFAULT_MOBILE_GATEWAY_LISTEN
        public_url = str(getattr(args, 'public_url', '') or '').strip() or None
        if not public_url:
            print("❌ Cloudflare Tunnel setup requires --public-url https://mobile.example.com.")
            return 1

        def _start_cloudflare_service():
            return start_or_replace_mobile_host_service(
                script_root=script_root,
                listen=listen,
                public_url=public_url,
                route_provider='cloudflare_tunnel',
                rotate_pairing=True,
            ).to_record()

        return run_mobile_cloudflare_onboarding(
            start_service_fn=_start_cloudflare_service,
        )

    def _start_service(commands, _status):
        mobile_serve = tuple(commands.mobile_serve)
        listen = _command_option(mobile_serve, '--listen') or DEFAULT_MOBILE_GATEWAY_LISTEN
        public_url = _command_option(mobile_serve, '--public-url')
        route_provider = _command_option(mobile_serve, '--route-provider') or 'tailnet'
        return start_or_replace_mobile_host_service(
            script_root=script_root,
            listen=listen,
            public_url=public_url,
            route_provider=route_provider,
            rotate_pairing=True,
        ).to_record()

    tailnet_listen = (
        str(getattr(args, 'listen', '') or '').strip()
        or DEFAULT_MOBILE_GATEWAY_LISTEN
    )
    return run_mobile_update_onboarding(
        start_service_fn=_start_service,
        listen=tailnet_listen,
    )


def _stream_is_tty(stream) -> bool:
    isatty = getattr(stream, 'isatty', None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def _read_stdio_prompt(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if line == '':
        raise EOFError
    return line.rstrip('\r\n')


def _command_option(command: tuple[str, ...], option: str) -> str | None:
    try:
        index = command.index(option)
    except ValueError:
        return None
    if index + 1 >= len(command):
        return None
    value = str(command[index + 1] or '').strip()
    return value or None


__all__ = ['POST_UPDATE_COMMAND', 'cmd_update', 'maybe_handle_post_update_command']
