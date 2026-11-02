#!/usr/bin/env python3
"""Package the explicit application allowlist, never a working-tree snapshot."""
from pathlib import Path
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updates

base = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]) if len(sys.argv) > 1 else base / 'release-dist/skill-dashboard.zip'
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in sorted(updates.FILES):
        archive.write(base / name, name)
print(output)
