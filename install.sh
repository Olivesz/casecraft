#!/usr/bin/env bash
# casecraft installer — sets up the venv, registers the MCP server with Claude,
# and installs the interviewer skill.
#
# Everything lives inside this directory plus ~/.claude and ~/.casecraft.
# Nothing is installed globally and no system Python is touched.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

say() { printf "\033[0;36m›\033[0m %s\n" "$1"; }
ok()  { printf "\033[0;32m✓\033[0m %s\n" "$1"; }
die() { printf "\033[0;31m✗\033[0m %s\n" "$1" >&2; exit 1; }

# ── 1. Python ────────────────────────────────────────────────────────────── #
if [ ! -x "$PY" ]; then
  BASE=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      v=$("$c" -c 'import sys; print(sys.version_info[:2] >= (3,11))' 2>/dev/null || echo False)
      [ "$v" = "True" ] && { BASE="$c"; break; }
    fi
  done
  [ -n "$BASE" ] || die "Need Python 3.11 or newer. Try: brew install python@3.12"
  say "Creating virtualenv with $BASE"
  "$BASE" -m venv "$VENV"
fi

say "Installing dependencies"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -e "$ROOT"
ok "Dependencies installed"

# ── 2. Sanity check ──────────────────────────────────────────────────────── #
"$PY" -m casecraft --check >/dev/null || die "Case library failed validation"
ok "Case library validated"

# ── 3. Skill ─────────────────────────────────────────────────────────────── #
SKILLS="$HOME/.claude/skills"
mkdir -p "$SKILLS"
rm -rf "$SKILLS/casecraft"
cp -R "$ROOT/skill/casecraft" "$SKILLS/casecraft"
ok "Interviewer skill installed to $SKILLS/casecraft"

# ── 4. Register the MCP server ───────────────────────────────────────────── #
if command -v claude >/dev/null 2>&1; then
  claude mcp remove casecraft --scope user >/dev/null 2>&1 || true
  if claude mcp add casecraft --scope user -- "$PY" -m casecraft >/dev/null 2>&1; then
    ok "MCP server registered with Claude Code (user scope)"
  else
    say "Could not auto-register. Add this manually:"
    echo "    claude mcp add casecraft --scope user -- $PY -m casecraft"
  fi
else
  say "Claude CLI not found. Register manually with:"
  echo "    claude mcp add casecraft --scope user -- $PY -m casecraft"
fi

# Claude Desktop config, for reference.
cat > "$ROOT/claude_desktop_snippet.json" <<EOF
{
  "mcpServers": {
    "casecraft": {
      "command": "$PY",
      "args": ["-m", "casecraft"]
    }
  }
}
EOF
ok "Claude Desktop snippet written to claude_desktop_snippet.json"

mkdir -p "$HOME/.casecraft/cases"

printf "\n\033[1mDone.\033[0m Start a new Claude session and say:\n\n"
printf "    \033[0;33mrun me through a case\033[0m\n\n"
printf "Your own cases go in ~/.casecraft/cases/ (see SCHEMA.md).\n"
