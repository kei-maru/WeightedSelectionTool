import io
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, UploadFile
from fastapi.responses import StreamingResponse

from services.api_services import api_service
from services.auth_service import auth_service


router = APIRouter(prefix="/api", dependencies=[Depends(auth_service.require_user)])


async def action(path, payload=None):
    return api_service.dispatch(path, payload or {})


@router.post("/state")
async def state():
    return await action("/api/state")


@router.post("/upload")
async def upload(file: UploadFile):
    return api_service.raffle.upload(file.filename, await file.read())


@router.post("/history/upload")
async def history_upload(file: UploadFile):
    return api_service.history.upload(file.filename, await file.read())


@router.post("/roles")
async def roles(payload: dict = Body(...)):
    return await action("/api/roles", payload)


@router.post("/raffle")
async def raffle(payload: dict = Body(...)):
    return await action("/api/raffle", payload)


@router.post("/session")
async def session(payload: dict = Body(...)):
    return await action("/api/session", payload)


@router.post("/session/delete")
async def session_delete(payload: dict = Body(...)):
    return await action("/api/session/delete", payload)


@router.post("/event/select")
async def event_select(payload: dict = Body(...)):
    return await action("/api/event/select", payload)


@router.post("/event", include_in_schema=False)
@router.post("/event/save")
async def event_save(payload: dict = Body(...)):
    return await action("/api/event/save", payload)


@router.post("/event/delete")
async def event_delete(payload: dict = Body(...)):
    return await action("/api/event/delete", payload)


@router.post("/user-event")
async def user_event(payload: dict = Body(...)):
    return await action("/api/user-event", payload)


@router.post("/history/apply")
async def history_apply(payload: dict = Body(...)):
    return await action("/api/history/apply", payload)


@router.post("/history/rollback")
async def history_rollback(payload: dict = Body(...)):
    return await action("/api/history/rollback", payload)


@router.post("/mode")
async def mode(payload: dict = Body(...)):
    return await action("/api/mode", payload)


@router.post("/special")
async def special(payload: dict = Body(...)):
    return await action("/api/special", payload)


@router.post("/exclude")
async def exclude(payload: dict = Body(...)):
    return await action("/api/exclude", payload)


@router.get("/export")
async def export(eventId: str = "__all__"):
    content, filename = api_service.exports.build_event_workbook(eventId)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
