#!/bin/bash
# Double-click to open Solfege on a Mac.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python is not installed. Get it from https://www.python.org/downloads/ then try again."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "First-time setup: creating a private Python environment..."
  python3 -m venv .venv || exit 1
fi

if [ ! -f ".venv/installed.txt" ]; then
  echo "Installing what Solfege needs. This happens once and takes a few minutes..."
  .venv/bin/python -m pip install --upgrade pip
  if ! .venv/bin/python -m pip install -r requirements.txt; then
    echo "Setup did not finish. Check your internet connection and run this again."
    read -n 1 -s -r -p "Press any key to close."
    exit 1
  fi
  echo done > .venv/installed.txt
fi

.venv/bin/python music_sorter.py
