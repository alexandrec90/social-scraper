r"""Windows toast notification utility.

Sends a Windows toast through the WinRT bridge in **Windows PowerShell 5.1**, so it
needs nothing installed. This used to `from win11toast import toast` behind a bare
`except Exception: pass`, and that package is not a dependency of this project and never
was — devkit's runtime dependency list is empty by contract, and a generated project
installs nothing for it either. So the import raised `ModuleNotFoundError` on every
call, the `except` swallowed it, and **no task has ever produced a toast** in any
project: `notify-wrap.py` wrapped 40-odd tasks and timed them for nothing.

Two things this file does differently as a result:

- **No third-party import.** The toast is built as XML and shown by
  `ToastNotificationManager`, reached through PowerShell. `pwsh` (PowerShell 7) cannot
  load a WinRT type at all — `Unable to find type
  [Windows.UI.Notifications.ToastNotificationManager…]` — so the interpreter is
  `System32\WindowsPowerShell\v1.0\powershell.exe` by absolute path, never whatever
  `powershell` resolves to on `PATH`.
- **A failure says so on stderr.** A toast must still never break the wrapped task, so
  every error is caught — but caught silently is what hid the above for the lifetime of
  the wrapper. `notify-wrap.py` runs inside the task's own terminal, which now persists
  per run, so one line there is seen without going anywhere near `logs/`.

`AppUserModelID` has to name an application Windows already knows, or the notifier
accepts the toast and shows nothing. Windows PowerShell's own id is the one identity
present on any machine that can run this at all; a devkit-specific id would need a
registered shortcut, which is an install step this file exists to avoid.

Called by `scripts/notify-wrap.py` — never imported by diagnostic scripts directly.
Notifications are a task-wrapper concern, not a script concern.
"""

import os
import subprocess
import sys
from xml.sax.saxutils import escape

# PowerShell 7 cannot load WinRT types, so resolve 5.1 by absolute path.
POWERSHELL = r"System32\WindowsPowerShell\v1.0\powershell.exe"

# An id Windows already knows; see the module docstring.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# Keeps a console-less parent from being given a visible console for this child. Safe
# here because the call captures both streams. Spelled the way every other spawn in this
# repo spells it, so the flag is 0 where the attribute does not exist.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The text arrives in the environment rather than in the argv: a task label carries
# quotes, brackets and em dashes, and none of those survive being pasted into a
# PowerShell string literal.
SHOW_TOAST = (
    "$ErrorActionPreference = 'Stop'; "
    "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType = WindowsRuntime]; "
    "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
    " ContentType = WindowsRuntime]; "
    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    "$xml.LoadXml($env:DEVKIT_TOAST_XML); "
    "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml; "
    "[Windows.UI.Notifications.ToastNotificationManager]"
    "::CreateToastNotifier($env:DEVKIT_TOAST_APPID).Show($toast)"
)

TIMEOUT_SECONDS = 20


def toast_xml(title: str, message: str) -> str:
    """The ToastGeneric payload, with both fields escaped.

    `Test: Harness Hook Tests — free` is a real task label and `&` a real character in
    another, so an unescaped title is not a hypothetical: `LoadXml` rejects the document
    and the toast is lost.
    """
    return (
        "<toast><visual><binding template='ToastGeneric'>"
        f"<text>{escape(title)}</text><text>{escape(message)}</text>"
        "</binding></visual></toast>"
    )


def powershell_path() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), POWERSHELL)


def notify(title: str, message: str) -> bool:
    """Show the toast. Returns whether it was shown; never raises."""
    if sys.platform != "win32":
        return False

    interpreter = powershell_path()
    if not os.path.exists(interpreter):
        print(f"notify: no Windows PowerShell at {interpreter}", file=sys.stderr)
        return False

    env = dict(os.environ)
    env["DEVKIT_TOAST_XML"] = toast_xml(title, message)
    env["DEVKIT_TOAST_APPID"] = APP_ID

    try:
        result = subprocess.run(
            [interpreter, "-NoProfile", "-NonInteractive", "-Command", SHOW_TOAST],
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except Exception as exc:  # a toast must never break the wrapped task
        print(f"notify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        print(f"notify: {detail[0] if detail else 'powershell failed'}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        notify(sys.argv[1], sys.argv[2])
