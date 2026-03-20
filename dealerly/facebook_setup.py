"""
dealerly/facebook_setup.py
==========================
Entry point for the Facebook Marketplace cookie setup helper.

Run once to log in and save your session cookies:

    python -m dealerly.facebook_setup

This module is a thin shim that delegates to facebook.facebook_setup().
"""
from dealerly.facebook import facebook_setup

if __name__ == "__main__":
    facebook_setup()
