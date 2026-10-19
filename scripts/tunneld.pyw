"""Run pymobiledevice3's tunneld with no window, logging to .state/tunneld.log.

Started by the "iPad tunneld" scheduled task (admin, at logon) that
scripts/install_tunneld_task.ps1 registers.
tunneld keeps one tunnel per iPad over USB or Wi-Fi and lists them on 127.0.0.1:49151.
pythonw has no console, so stdout and stderr go to a log file that is cut back to
its last 2 MB at each start.

STOPGAP until pymobiledevice3 fixes it upstream (doronz88/pymobiledevice3#2051): tunneld runs inside
uvicorn with loop="asyncio", which on Windows always builds a ProactorEventLoop, and
the Bonjour browse that finds the iPad on Wi-Fi gets no answers on that loop
(checked 2026-10-18: found in 0 of 14 browses on the proactor loop, 11 of 16 on the selector).
pymobiledevice3 already sets the selector policy for itself; loop="none" makes uvicorn
use it. Remove this patch once a pymobiledevice3 release passes loop="none" on Windows.
"""

import sys
from pathlib import Path

LOG = Path(__file__).resolve().parents[1] / ".state" / "tunneld.log"
KEEP = 2 * 1024 * 1024

LOG.parent.mkdir(parents=True, exist_ok=True)
if LOG.exists() and LOG.stat().st_size > KEEP:
    LOG.write_bytes(LOG.read_bytes()[-KEEP:])
log = open(LOG, "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log

import uvicorn  # noqa: E402

_run = uvicorn.run


def _run_with_policy_loop(*args, **kwargs):
    if sys.platform == "win32" and kwargs.get("loop") == "asyncio":
        kwargs["loop"] = "none"
    return _run(*args, **kwargs)


uvicorn.run = _run_with_policy_loop

from pymobiledevice3.__main__ import main  # noqa: E402

sys.argv = ["pymobiledevice3", "remote", "tunneld"]
sys.exit(main())
