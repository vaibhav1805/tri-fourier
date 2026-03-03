#!/usr/bin/env bash
#
# dev-setup.sh -- One-command local development environment for AutoTriage
#
# Usage:
#   ./scripts/dev-setup.sh          # Full setup: venv + deps + run dev server
#   ./scripts/dev-setup.sh --run    # Skip setup, just run dev server
#   ./scripts/dev-setup.sh --setup  # Setup only, don't start server
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
RUN_ONLY=false
SETUP_ONLY=false

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

for arg in "$@"; do
  case $arg in
    --run) RUN_ONLY=true ;;
    --setup) SETUP_ONLY=true ;;
    --help|-h)
      echo "Usage: $0 [--run] [--setup]"
      echo "  --run    Skip setup, just start the dev server"
      echo "  --setup  Setup only, don't start the server"
      exit 0
      ;;
  esac
done

cd "$PROJECT_DIR"

if [ "$RUN_ONLY" = false ]; then
  echo -e "${BLUE}"
  echo "  AutoTriage Local Dev Setup"
  echo "  =========================="
  echo -e "${NC}"

  # Step 1: Python version check
  log_info "Checking Python version..."
  PYTHON_VERSION=$(python3 --version 2>/dev/null | awk '{print $2}' || echo "none")
  MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
  MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
  if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 12 ]; }; then
    log_warn "Python 3.12+ required (found: $PYTHON_VERSION)"
    log_warn "Install via: brew install python@3.12 or pyenv install 3.12"
    exit 1
  fi
  log_ok "Python $PYTHON_VERSION"

  # Step 2: Virtual environment
  if [ ! -d "$VENV_DIR" ]; then
    log_info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    log_ok "Virtual environment created at .venv/"
  else
    log_ok "Virtual environment exists"
  fi

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"

  # Step 3: Install dependencies
  log_info "Installing dependencies..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  if [ -f requirements-dev.txt ]; then
    pip install --quiet -r requirements-dev.txt
  fi
  log_ok "Dependencies installed"

  # Step 4: Create data directories
  mkdir -p data/graph data/snapshots logs
  log_ok "Data directories ready"

  # Step 5: Create .env if missing
  if [ ! -f .env ]; then
    if [ -f .env.example ]; then
      cp .env.example .env
      log_warn "Created .env from .env.example -- edit with your credentials"
    fi
  else
    log_ok ".env file exists"
  fi

  echo ""
  log_ok "Setup complete!"
  echo ""
fi

if [ "$SETUP_ONLY" = true ]; then
  echo "Run the dev server with:"
  echo "  source .venv/bin/activate"
  echo "  uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000"
  exit 0
fi

# Start dev server with auto-reload
echo -e "${GREEN}Starting dev server with auto-reload...${NC}"
echo "  API:    http://localhost:8000"
echo "  Docs:   http://localhost:8000/docs"
echo "  Health: http://localhost:8000/health"
echo ""

# Activate venv if not already active
if [ -z "${VIRTUAL_ENV:-}" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
fi

exec uvicorn src.api.main:app \
  --reload \
  --reload-dir src \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level "${LOG_LEVEL:-debug}"
