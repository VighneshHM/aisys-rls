"""HTTP API for shelves and the virtual bookshelf. Reading needs `catalogue.read`; changing a shelf `shelves.manage`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aisys.deps import Principal, client_ip, get_db, require
from aisys.shelves import service

router = APIRouter(prefix="/api/shelves", tags=["shelves"])
CanRead = Depends(require("catalogue.read"))
CanManage = Depends(require("shelves.manage"))


def _http(exc: service.ShelfError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": []})


class ShelfIn(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=200)
    capacity: int | None = Field(default=None, ge=0, le=100_000, strict=True)


class ShelfPatch(BaseModel):
    description: str | None = Field(default=None, max_length=200)
    capacity: int | None = Field(default=None, ge=0, le=100_000, strict=True)


@router.get("")
def list_shelves(_: Principal = CanRead, db: Session = Depends(get_db)):
    return {"shelves": service.list_shelves(db)}


@router.get("/view")
def view_shelf(
    code: str, limit: int = service.DEFAULT_VIEW, offset: int = 0, _: Principal = CanRead, db: Session = Depends(get_db)
):
    """The books on one shelf, in call-number order. The code is a query value because it may contain slashes."""
    try:
        return service.view(db, code, limit, offset)
    except service.ShelfError as exc:
        raise _http(exc) from exc


@router.post("", status_code=201)
def create_shelf(body: ShelfIn, request: Request, principal: Principal = CanManage, db: Session = Depends(get_db)):
    try:
        return service.create_shelf(
            db, body.code, body.description, body.capacity, actor=principal.user.username, ip=client_ip(request)
        )
    except service.ShelfError as exc:
        raise _http(exc) from exc


@router.patch("/{shelf_id}")
def update_shelf(
    shelf_id: int, body: ShelfPatch, request: Request, principal: Principal = CanManage, db: Session = Depends(get_db)
):
    try:
        return service.update_shelf(
            db,
            shelf_id,
            body.model_dump(exclude_unset=True),
            actor=principal.user.username,
            ip=client_ip(request),
        )
    except service.ShelfError as exc:
        raise _http(exc) from exc


@router.delete("/{shelf_id}", status_code=204)
def delete_shelf(shelf_id: int, request: Request, principal: Principal = CanManage, db: Session = Depends(get_db)):
    try:
        service.delete_shelf(db, shelf_id, actor=principal.user.username, ip=client_ip(request))
    except service.ShelfError as exc:
        raise _http(exc) from exc
