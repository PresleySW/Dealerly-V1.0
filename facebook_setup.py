"""
dealerly/facebook_setup.py
==========================
Entry point for the Facebook Marketplace cookie setup helper.

Run once to log in and save your session cookies (from a normal shell, in the
`Dealerly 1.0` folder that contains `dealerly/`):

    python -m dealerly.facebook_setup

**Windows PowerShell 5.x** (default on many PCs): do **not** use ``!`` (that is
Jupyter-only). ``&&`` is not valid in PS 5.x — use ``;`` or run ``cd`` on its
own line::

    cd "d:/path/to/Dealerly 1.0"
    python -m dealerly.facebook_setup

One line::

    Set-Location "d:/path/to/Dealerly 1.0"; python -m dealerly.facebook_setup

**cmd.exe** (classic Command Prompt): ``cd /d ... && python ...`` is OK.

**Jupyter / IPython:** use ``%cd`` and ``!python`` (see above); not for PowerShell.

This module is a thin shim that delegates to facebook.facebook_setup().
"""
from dealerly.facebook import facebook_setup

if __name__ == "__main__":
    facebook_setup()
