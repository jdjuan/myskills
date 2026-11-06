#!/bin/sh
set -eu
[ "$(uname -s)" = Darwin ] || { echo 'Use install.ps1 on Windows. This installer supports macOS.' >&2; exit 1; }
DASH_HOME=${SKILL_DASHBOARD_HOME:-"$HOME/Library/Application Support/SkillDashboard"}
export SKILL_DASHBOARD_HOME="$DASH_HOME"
export UV_UNMANAGED_INSTALL="$DASH_HOME/uv"
export UV_PYTHON_INSTALL_DIR="$DASH_HOME/runtime"
export UV_PYTHON_INSTALL_BIN=0
mkdir -p "$DASH_HOME"
if [ ! -x "$DASH_HOME/uv/uv" ]; then
  curl --fail --location --silent --show-error https://astral.sh/uv/install.sh | sh
fi
"$DASH_HOME/uv/uv" python install 3.12 --no-config --no-bin
DASH_PYTHON=$("$DASH_HOME/uv/uv" python find --managed-python 3.12)
DASH_WORK=$(mktemp -d)
trap 'rm -rf "$DASH_WORK"' EXIT
curl --fail --location --silent --show-error https://github.com/jdjuan/myskills/releases/latest/download/myskills.zip -o "$DASH_WORK/app.zip"
/usr/bin/ditto -x -k "$DASH_WORK/app.zip" "$DASH_WORK/app"
"$DASH_PYTHON" "$DASH_WORK/app/dashboard.py" install --home "$DASH_HOME" "$@"
