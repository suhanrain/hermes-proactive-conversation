# -*- coding: utf-8 -*-
"""pytest bootstrap for the public test tree.

This file is intentionally free of any machine-specific path. The plugin's own
fixtures live in ``tests/conftest.py``; when the suite is run inside a host
checkout, that fixture loads the host's scripted model provider from the host
source tree. Add that checkout to ``sys.path`` (or run from it) if you need it.
"""
