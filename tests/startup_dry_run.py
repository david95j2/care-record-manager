"""Run task definition validation with -WhatIf; never register or start a task."""
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from store import Store
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

with tempfile.TemporaryDirectory(prefix='activity-task-check-') as folder:
    store=Store(folder)
    script=Path(__file__).resolve().parents[1]/'startup-task.ps1'
    result=subprocess.run(['powershell.exe','-NoProfile','-File',str(script),'-Action','Install',
                           '-Python',sys.executable,'-DataPath',folder,'-TaskName','ActivitySupportValidationOnly','-WhatIf'],
                          capture_output=True,timeout=30)
    print(result.stdout.decode('utf-8',errors='replace'))
    if result.returncode:
        print(result.stderr.decode(errors='replace'));raise SystemExit(result.returncode)
    print('Startup dry-run passed: no scheduled task registered.')
