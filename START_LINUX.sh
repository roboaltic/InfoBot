#!/usr/bin/env bash
set -e
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    echo 'Для графічної форми потрібен Tkinter.' >&2
    echo 'Ubuntu/Debian: sudo apt-get install python3-tk' >&2
    exit 1
fi
exec python3 main.py
