#!/bin/sh
set -eu

usage() {
    printf '%s\n' "Usage: ./install.sh [--prefix ABSOLUTE_PATH]"
}

reconnect_prefix="${HOME}/.local"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            if [ "$#" -lt 2 ]; then
                usage >&2
                exit 2
            fi
            reconnect_prefix=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$reconnect_prefix" in
    /*) ;;
    *)
        printf '%s\n' "--prefix must be an absolute path" >&2
        exit 2
        ;;
esac

reconnect_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
reconnect_share_parent="$reconnect_prefix/share"
reconnect_app="$reconnect_share_parent/codex-reconnect"
reconnect_bin_dir="$reconnect_prefix/bin"
reconnect_bin="$reconnect_bin_dir/codex-reconnect"
reconnect_skill_parent="${HOME}/.agents/skills"
reconnect_skill="$reconnect_skill_parent/reconnect"
reconnect_skill_target="$reconnect_app/skills/reconnect"
reconnect_stage=""
reconnect_backup=""
reconnect_link_stage=""
reconnect_skill_link_stage=""
reconnect_new_installed=0
reconnect_skill_created=0
reconnect_command_preexisting=0

for reconnect_required in \
    "$reconnect_source/codex-reconnect" \
    "$reconnect_source/codex_reconnect" \
    "$reconnect_source/skills/reconnect/SKILL.md" \
    "$reconnect_source/skills/reconnect/agents/openai.yaml" \
    "$reconnect_source/README.md" \
    "$reconnect_source/README.zh-CN.md" \
    "$reconnect_source/uninstall.sh"
do
    if [ ! -e "$reconnect_required" ]; then
        printf 'Missing required install source: %s\n' "$reconnect_required" >&2
        exit 3
    fi
done

mkdir -p "$reconnect_share_parent" "$reconnect_bin_dir" "$reconnect_skill_parent"

if [ -e "$reconnect_bin" ] && [ ! -L "$reconnect_bin" ]; then
    printf 'Refusing to replace non-symlink command: %s\n' "$reconnect_bin" >&2
    exit 3
fi
if [ -L "$reconnect_bin" ]; then
    reconnect_existing_command_target=$(readlink "$reconnect_bin")
    if [ "$reconnect_existing_command_target" != "$reconnect_app/codex-reconnect" ]; then
        printf 'Refusing to replace command linked elsewhere: %s -> %s\n' \
            "$reconnect_bin" "$reconnect_existing_command_target" >&2
        exit 3
    fi
    reconnect_command_preexisting=1
fi
if [ -L "$reconnect_app" ]; then
    printf 'Refusing to replace symlink application directory: %s\n' "$reconnect_app" >&2
    exit 3
fi
if [ -e "$reconnect_app" ] && [ ! -d "$reconnect_app" ]; then
    printf 'Refusing to replace non-directory application path: %s\n' "$reconnect_app" >&2
    exit 3
fi
if [ -e "$reconnect_skill" ] || [ -L "$reconnect_skill" ]; then
    if [ ! -L "$reconnect_skill" ]; then
        printf 'Refusing to replace non-symlink user skill: %s\n' "$reconnect_skill" >&2
        exit 3
    fi
    reconnect_existing_skill_target=$(readlink "$reconnect_skill")
    if [ "$reconnect_existing_skill_target" != "$reconnect_skill_target" ]; then
        printf 'Refusing to replace user skill linked elsewhere: %s -> %s\n' \
            "$reconnect_skill" "$reconnect_existing_skill_target" >&2
        exit 3
    fi
fi

cleanup() {
    reconnect_status=$?
    trap - EXIT HUP INT TERM
    if [ "$reconnect_status" -ne 0 ]; then
        if [ "$reconnect_new_installed" -eq 1 ] && [ -d "$reconnect_app" ]; then
            rm -rf -- "$reconnect_app"
        fi
        if [ -n "$reconnect_backup" ] && [ -d "$reconnect_backup" ]; then
            mv -- "$reconnect_backup" "$reconnect_app"
        fi
        if [ "$reconnect_skill_created" -eq 1 ] && [ -L "$reconnect_skill" ]; then
            rm -- "$reconnect_skill"
        fi
        if [ "$reconnect_command_preexisting" -eq 0 ] && [ -L "$reconnect_bin" ]; then
            reconnect_failed_command_target=$(readlink "$reconnect_bin")
            if [ "$reconnect_failed_command_target" = "$reconnect_app/codex-reconnect" ]; then
                rm -- "$reconnect_bin"
            fi
        fi
    elif [ -n "$reconnect_backup" ] && [ -d "$reconnect_backup" ]; then
        rm -rf -- "$reconnect_backup"
    fi
    if [ -n "$reconnect_stage" ] && [ -d "$reconnect_stage" ]; then
        rm -rf -- "$reconnect_stage"
    fi
    if [ -n "$reconnect_link_stage" ] && [ -L "$reconnect_link_stage" ]; then
        rm -- "$reconnect_link_stage"
    fi
    if [ -n "$reconnect_skill_link_stage" ] && [ -L "$reconnect_skill_link_stage" ]; then
        rm -- "$reconnect_skill_link_stage"
    fi
    exit "$reconnect_status"
}
trap cleanup EXIT HUP INT TERM

reconnect_stage=$(mktemp -d "$reconnect_share_parent/.codex-reconnect.install.XXXXXX")
install -m 755 "$reconnect_source/codex-reconnect" "$reconnect_stage/codex-reconnect"
install -m 755 "$reconnect_source/uninstall.sh" "$reconnect_stage/uninstall.sh"
install -m 644 "$reconnect_source/README.md" "$reconnect_stage/README.md"
install -m 644 "$reconnect_source/README.zh-CN.md" "$reconnect_stage/README.zh-CN.md"
cp -R "$reconnect_source/codex_reconnect" "$reconnect_stage/codex_reconnect"
cp -R "$reconnect_source/skills" "$reconnect_stage/skills"
find "$reconnect_stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$reconnect_stage" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$reconnect_stage/codex_reconnect" "$reconnect_stage/skills" -type d -exec chmod 755 {} +
find "$reconnect_stage/codex_reconnect" "$reconnect_stage/skills" -type f -exec chmod 644 {} +

if [ -d "$reconnect_app" ]; then
    reconnect_backup=$(mktemp -d "$reconnect_share_parent/.codex-reconnect.backup.XXXXXX")
    rmdir -- "$reconnect_backup"
    mv -- "$reconnect_app" "$reconnect_backup"
fi
mv -- "$reconnect_stage" "$reconnect_app"
reconnect_stage=""
reconnect_new_installed=1

reconnect_link_stage="$reconnect_bin_dir/.codex-reconnect.link.$$"
if [ -e "$reconnect_link_stage" ] || [ -L "$reconnect_link_stage" ]; then
    printf 'Temporary link path already exists: %s\n' "$reconnect_link_stage" >&2
    exit 3
fi
ln -s "$reconnect_app/codex-reconnect" "$reconnect_link_stage"
mv -f -- "$reconnect_link_stage" "$reconnect_bin"
reconnect_link_stage=""

if [ ! -L "$reconnect_skill" ]; then
    reconnect_skill_link_stage="$reconnect_skill_parent/.reconnect.link.$$"
    if [ -e "$reconnect_skill_link_stage" ] || [ -L "$reconnect_skill_link_stage" ]; then
        printf 'Temporary skill link path already exists: %s\n' \
            "$reconnect_skill_link_stage" >&2
        exit 3
    fi
    ln -s "$reconnect_skill_target" "$reconnect_skill_link_stage"
    mv -- "$reconnect_skill_link_stage" "$reconnect_skill"
    reconnect_skill_link_stage=""
    reconnect_skill_created=1
fi

printf 'Installed codex-reconnect to %s\n' "$reconnect_app"
printf 'Command: %s\n' "$reconnect_bin"
printf 'Codex skill: %s\n' "$reconnect_skill"
