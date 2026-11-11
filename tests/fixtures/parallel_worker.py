"""Run an independent V4 writer after a real process barrier."""
import importlib.util
import json
from pathlib import Path
import sys
import time

script, project, request_file, ready, gate = sys.argv[1:]
spec = importlib.util.spec_from_file_location('independent_v4_worker', script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
runtime = module.Runtime(project)
request = json.loads(Path(request_file).read_text(encoding='utf-8'))
Path(ready).write_text('ready', encoding='ascii')
deadline = time.monotonic() + 15
while not Path(gate).exists():
    if time.monotonic() >= deadline:
        raise SystemExit('Barrier was not released')
    time.sleep(0.005)
try:
    response = runtime.transact(request)
except Exception as error:
    print(json.dumps({'error': str(error), 'code': getattr(error, 'code', type(error).__name__)}))
    raise SystemExit(1)
print(json.dumps({'revision': response['revision'], 'results': response['results'], 'replayed': response['replayed']}))
