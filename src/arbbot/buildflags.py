"""Compile-time capability flags.

This module is deliberately *not* configurable by environment variable. FR-016
requires live order submission to sit behind a build flag, a runtime flag, and
a per-basket human approval; if all three were runtime settings, a single
mis-set variable in a deployment would be enough to arm the system.

Flipping :data:`LIVE_EXECUTION_COMPILED_IN` therefore requires editing source,
opening a pull request, and cutting a release -- an act that is reviewable and
permanently recorded in version control. That is the intended cost.

Do not read these values from ``os.environ``. Do not add a setter.
"""

from __future__ import annotations

from typing import Final

#: Whether this build contains an armed live-execution path at all.
#: Stays ``False`` until the Milestone 5 release. See docs/adr/0004.
LIVE_EXECUTION_COMPILED_IN: Final[bool] = False

#: Whether this build may place orders against the venue demo environment.
#: Enabled at Milestone 4; demo orders spend mock funds only.
DEMO_EXECUTION_COMPILED_IN: Final[bool] = False
