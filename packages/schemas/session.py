from packages.schemas.common import ORMModel


class SessionResponse(ORMModel):
    session_id: str
    status: str

