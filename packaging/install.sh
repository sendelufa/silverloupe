#!/bin/sh
set -eu

prerequisite_error() {
    printf '%s\n' "$1" >&2
    exit 2
}

stage_error() {
    printf '%s\n' "$1" >&2
    exit 3
}

temporary_directory=""
cleanup() {
    if [ -n "$temporary_directory" ] && [ -d "$temporary_directory" ]; then
        rm -rf -- "$temporary_directory"
    fi
}
on_signal() {
    exit 130
}
trap cleanup EXIT
trap on_signal HUP INT TERM

system_name=$(uname -s)
machine_name=$(uname -m)
if [ "$system_name" != "Darwin" ] || {
    [ "$machine_name" != "arm64" ] && [ "$machine_name" != "aarch64" ];
}; then
    prerequisite_error "Silverloupe requires macOS on Apple Silicon."
fi

codex_path=$(command -v codex 2>/dev/null || true)
claude_path=$(command -v claude 2>/dev/null || true)
if [ -z "$codex_path" ] && [ -z "$claude_path" ]; then
    prerequisite_error "Codex or Claude Code was not found. Install an agent CLI first:\n  Codex: https://developers.openai.com/codex/cli/\n  Claude Code: https://docs.anthropic.com/en/docs/claude-code"
fi

printf 'Silverloupe setup\n'
if [ -n "$codex_path" ]; then
    printf '  Found Codex: %s\n' "$codex_path"
fi
if [ -n "$claude_path" ]; then
    printf '  Found Claude Code: %s\n' "$claude_path"
fi

temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/silverloupe-install.XXXXXX") ||
    stage_error "Could not create a temporary installation directory."

release_url=${SILVERLOUPE_RELEASE_URL:-https://github.com/sendelufa/silverloupe/releases/latest/download/silverloupe-latest.tar.gz}
checksum_url=${SILVERLOUPE_RELEASE_SHA256_URL:-${release_url}.sha256}
archive_name=${release_url##*/}
archive_name=${archive_name%%\?*}
if [ -z "$archive_name" ] || [ "$archive_name" = "." ] || [ "$archive_name" = ".." ]; then
    stage_error "The release URL does not contain a safe archive name."
fi
case "$archive_name" in
    */*|*..*) stage_error "The release URL contains an unsafe archive name." ;;
esac

archive_path=$temporary_directory/$archive_name
checksum_path=$temporary_directory/$archive_name.sha256.download
if ! curl -fsSL "$release_url" -o "$archive_path"; then
    stage_error "Could not download the Silverloupe release."
fi
if ! curl -fsSL "$checksum_url" -o "$checksum_path"; then
    stage_error "Could not download the Silverloupe checksum."
fi

checksum_line=""
second_line=""
{
    IFS= read -r checksum_line || true
    IFS= read -r second_line || true
} < "$checksum_path"
set -- $checksum_line
if [ "$#" -ne 2 ] || [ -n "$second_line" ]; then
    stage_error "The Silverloupe checksum file is invalid."
fi
expected_hash=$1
expected_name=$2
case "$expected_hash" in
    ''|*[!0123456789abcdefABCDEF]*) stage_error "The Silverloupe checksum is invalid." ;;
esac
if [ "${#expected_hash}" -ne 64 ] || [ "$expected_name" != "$archive_name" ]; then
    stage_error "The Silverloupe checksum does not match the requested archive."
fi
verified_checksum=$temporary_directory/$archive_name.sha256
printf '%s  %s\n' "$expected_hash" "$archive_name" > "$verified_checksum"
if ! (cd "$temporary_directory" && shasum -a 256 -c "$archive_name.sha256" >/dev/null); then
    stage_error "The Silverloupe archive checksum does not match."
fi

extracted=$temporary_directory/extracted
mkdir -p "$extracted"
if ! tar -xzf "$archive_path" -C "$extracted"; then
    stage_error "The Silverloupe archive could not be extracted."
fi
install_list=$temporary_directory/install-files
find "$extracted" -type f -name install.py -print > "$install_list"
install_py=""
while IFS= read -r candidate; do
    if [ -n "$install_py" ]; then
        stage_error "The Silverloupe archive contains multiple installers."
    fi
    install_py=$candidate
done < "$install_list"
if [ -z "$install_py" ]; then
    stage_error "The Silverloupe archive does not contain install.py."
fi
release_root=${install_py%/install.py}
requirements=$release_root/runtime/requirements.lock
if [ ! -f "$requirements" ]; then
    stage_error "The Silverloupe runtime lock is missing."
fi

prompt_yes() {
    prompt_text=$1
    if [ ! -r /dev/tty ]; then
        prerequisite_error "An interactive Terminal is required."
    fi
    printf '%s [Y/n] ' "$prompt_text" > /dev/tty
    answer=""
    if ! IFS= read -r answer < /dev/tty; then
        prerequisite_error "Could not read an answer from the Terminal."
    fi
    case "$answer" in
        ''|y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

uv_path=$(command -v uv 2>/dev/null || true)
if [ -z "$uv_path" ]; then
    printf 'uv is required to manage an isolated Python 3.12 runtime.\n'
    printf 'It will remain installed if you later cancel Silverloupe.\n'
    if ! prompt_yes "Install uv now?"; then
        printf 'Installation cancelled; no Silverloupe files were installed.\n'
        exit 0
    fi
    uv_installer_url=${SILVERLOUPE_UV_INSTALLER_URL:-https://astral.sh/uv/install.sh}
    uv_installer=$temporary_directory/uv-install.sh
    if ! curl -fsSL "$uv_installer_url" -o "$uv_installer"; then
        stage_error "Could not download the official uv installer."
    fi
    if ! /bin/sh "$uv_installer"; then
        stage_error "The official uv installer failed."
    fi
    uv_path=$(command -v uv 2>/dev/null || true)
    if [ -z "$uv_path" ] && [ -x "$HOME/.local/bin/uv" ]; then
        uv_path=$HOME/.local/bin/uv
    fi
    if [ -z "$uv_path" ]; then
        stage_error "uv was installed but its executable could not be found."
    fi
fi

case "$uv_path" in
    /*)
        uv_directory=${uv_path%/*}
        PATH=$uv_directory:$PATH
        export PATH
        SILVERLOUPE_UV_EXECUTABLE=$uv_path
        export SILVERLOUPE_UV_EXECUTABLE
        ;;
    *) stage_error "The resolved uv executable path is not absolute." ;;
esac

if ! "$uv_path" python find 3.12 >/dev/null 2>&1; then
    printf 'Python 3.12 is required and will be managed by uv.\n'
    printf 'It will remain installed if you later cancel Silverloupe.\n'
    if ! prompt_yes "Install Python 3.12 now?"; then
        printf 'Installation cancelled; no Silverloupe files were installed.\n'
        exit 0
    fi
    if ! "$uv_path" python install 3.12; then
        stage_error "uv could not install Python 3.12."
    fi
fi

if [ ! -r /dev/tty ]; then
    prerequisite_error "An interactive Terminal is required."
fi
"$uv_path" run --python 3.12 --no-project \
    --with-requirements "$requirements" \
    python "$install_py" < /dev/tty
