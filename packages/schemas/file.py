from packages.schemas.common import ORMModel


class UploadedFileResponse(ORMModel):
    file_id: str
    file_type: str
    storage_key: str
    original_name: str

