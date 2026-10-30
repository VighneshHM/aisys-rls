"""HTTP API for the Updates page: what is installed, the history, and a read-only check of a waiting package.

Applying an update or a rollback restarts the application, so those stay on the command line (`manage.py`). The
page only reads, and `check` never changes anything. Needs `admin.config`.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shelfsense import __version__
from shelfsense.deps import Principal, require
from shelfsense_lifecycle import ops
from shelfsense_lifecycle.bundle import LifecycleError

router = APIRouter(prefix="/api/updates", tags=["updates"])
CanAdmin = Depends(require("admin.config"))
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def lifecycle_root() -> ops.Root | None:
    """The installation folder this copy of the app was started from, or None when run straight from the source tree."""
    home = os.environ.get("SHELFSENSE_HOME", "")
    if home and (Path(home) / "state.json").is_file():
        return ops.Root(home)
    return None


class CheckIn(BaseModel):
    name: str = Field(max_length=200)


@router.get("/status")
def status(_: Principal = CanAdmin):
    root = lifecycle_root()
    base = {"running_version": __version__, "python": sys.version.split()[0]}
    if root is None:
        return {**base, "managed": False}
    s = ops.status(root)
    s.pop("running", None)
    return {**base, "managed": True, **s}


@router.post("/check")
def check(body: CheckIn, _: Principal = CanAdmin):
    """Dry run of an update for a package waiting in the incoming folder: verifies everything, changes nothing."""
    root = lifecycle_root()
    if root is None:
        raise HTTPException(
            409, detail={"message": "This copy was not installed from a package, so there is nothing to update."}
        )
    path = (root.incoming / body.name).resolve()
    if not PACKAGE_NAME.match(body.name) or path.parent != root.incoming.resolve() or not path.exists():
        raise HTTPException(404, detail={"message": f"No package called '{body.name}' in the incoming folder."})
    try:
        bundle, plan = ops.plan_update(root, path)
    except LifecycleError as exc:
        raise HTTPException(422, detail={"message": str(exc)}) from exc
    bundle.close()
    return plan
