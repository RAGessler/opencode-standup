#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_root="${OPENCODE_STANDUP_INSTALL_ROOT:-${HOME}/.local/share/opencode-standup}"
venv="${install_root}/venv"
bin_dir="${HOME}/.local/bin"

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' "python3 is required but was not found on PATH." >&2
  exit 1
fi

python3 -c 'import sys; sys.exit("Python 3.10 or newer is required.") if sys.version_info < (3, 10) else None'

mkdir -p "$install_root" "$bin_dir"

if [[ ! -x "${venv}/bin/python" ]]; then
  python3 -m venv "$venv"
fi

"${venv}/bin/python" -m pip install --editable "$repo_root"

cat > "${bin_dir}/opencode-standup" <<EOF
#!/usr/bin/env bash
exec "${venv}/bin/opencode-standup" "\$@"
EOF
chmod +x "${bin_dir}/opencode-standup"

printf 'Installed opencode-standup from %s\n' "$repo_root"
printf 'Launcher: %s\n' "${bin_dir}/opencode-standup"
