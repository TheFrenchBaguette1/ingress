#!/bin/zsh
# Double-click on macOS to set up the SPHEREx backend's Python environment.
set -e
cd -- "$(dirname -- "$0")"

if command -v python3.11 >/dev/null 2>&1; then
  python_executable=$(command -v python3.11)
elif [[ -x /opt/homebrew/bin/python3.11 ]]; then
  python_executable=/opt/homebrew/bin/python3.11
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] < (3, 15)))'; then
  python_executable=$(command -v python3)
else
  echo "Python 3.11 through 3.14 is required. Install Python 3.11, then run this file again."
  read '?Press Return to close this window.'
  exit 1
fi

echo "Using $python_executable"
if [[ ! -x .venv/bin/python ]]; then
  "$python_executable" -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] < (3, 15)))'; then
  echo "The existing .venv uses an unsupported Python version. Rename .venv and run setup again."
  read '?Press Return to close this window.'
  exit 1
fi

echo "Installing Python dependencies from requirements.txt..."
if .venv/bin/python -m pip install -r requirements.txt &&
  .venv/bin/python -c 'import astropy, astroquery, matplotlib, numpy, PIL, reproject, fastapi, pydantic, uvicorn'; then
  echo "Setup complete. Run the two commands in README.md to start the app."
else
  echo "Setup did not finish. Check your internet connection and the error above, then run this file again."
  read '?Press Return to close this window.'
  exit 1
fi
read '?Press Return to close this window.'
