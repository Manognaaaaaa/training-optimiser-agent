"""Errors that services raise. main.py turns them into JSON with a readable ``detail`` string."""


class ApiError(Exception):
    status_code = 400

    def __init__(self, detail: str, errors: dict[str, str] | None = None, extra: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or {}  # optional {field: message} so forms can mark the bad field
        self.extra = extra or {}  # optional extra JSON keys (for example the checker's violations)


class NotFound(ApiError):
    status_code = 404


class Conflict(ApiError):
    """The request is valid but clashes with current data (duplicate code, rule clash, blocked delete)."""

    status_code = 409


class Invalid(ApiError):
    """The input itself is not acceptable."""

    status_code = 422


def get_or_404(db, model, obj_id: int, label: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{label} {obj_id} not found")
    return obj
