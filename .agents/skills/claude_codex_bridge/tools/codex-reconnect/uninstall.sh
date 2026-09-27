#!/bin/sh
set -eu

usage() {
    printf '%s\n' "Usage: ./uninstall.sh [--prefix ABSOLUTE_PATH]"
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

reconnect_app="$reconnect_prefix/share/codex-reconnect"
reconnect_bin="$reconnect_prefix/bin/codex-reconnect"
reconnect_skill="${HOME}/.agents/skills/reconnect"
reconnect_skill_target="$reconnect_app/skills/reconnect"

if [ -L "$reconnect_app" ]; then
    printf 'Refusing to remove symlink application directory: %s\n' "$reconnect_app" >&2
    exit 3
fi
if [ -e "$reconnect_app" ] && [ ! -d "$reconnect_app" ]; then
    printf 'Refusing to remove non-directory application path: %s\n' "$reconnect_app" >&2
    exit 3
fi
if [ -e "$reconnect_bin" ] && [ ! -L "$reconnect_bin" ]; then
    printf 'Refusing to remove non-symlink command: %s\n' "$reconnect_bin" >&2
    exit 3
fi
if [ -L "$reconnect_bin" ]; then
    reconnect_target=$(readlink "$reconnect_bin")
    if [ "$reconnect_target" != "$reconnect_app/codex-reconnect" ]; then
        printf 'Refusing to remove command linked elsewhere: %s -> %s\n' \
            "$reconnect_bin" "$reconnect_target" >&2
        exit 3
    fi
    rm -- "$reconnect_bin"
fi

if [ -L "$reconnect_skill" ]; then
    reconnect_existing_skill_target=$(readlink "$reconnect_skill")
    if [ "$reconnect_existing_skill_target" = "$reconnect_skill_target" ]; then
        rm -- "$reconnect_skill"
    else
        printf 'Preserved user skill linked elsewhere: %s -> %s\n' \
            "$reconnect_skill" "$reconnect_existing_skill_target"
    fi
elif [ -e "$reconnect_skill" ]; then
    printf 'Preserved non-symlink user skill: %s\n' "$reconnect_skill"
fi

if [ -d "$reconnect_app" ]; then
    rm -rf -- "$reconnect_app"
fi

printf 'Uninstalled codex-reconnect from %s\n' "$reconnect_prefix"
printf '%s\n' "State and audit logs were kept."
