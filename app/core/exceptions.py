"""Domain exceptions. They carry an HTTP status so the API layer can map them uniformly."""


class AppError(Exception):
    status_code: int = 400
    code: str = "BAD_REQUEST"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnsupportedFileType(AppError):
    status_code = 415
    code = "UNSUPPORTED_FILE_TYPE"


class FileTooLarge(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"


class InvalidUpload(AppError):
    """The upload is structurally wrong (bad zip, missing shapefile parts, unsafe paths...)."""

    status_code = 422
    code = "INVALID_UPLOAD"


class FileNotFound(AppError):
    status_code = 404
    code = "FILE_NOT_FOUND"


class FileNotReady(AppError):
    status_code = 409
    code = "FILE_NOT_READY"


class InvalidQuery(AppError):
    status_code = 422
    code = "INVALID_QUERY"
