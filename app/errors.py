from fastapi import HTTPException


class APIError(HTTPException):
    """业务错误，统一输出 {"error": code, "message": ..., "detail": ...}。"""

    def __init__(self, status_code: int, code: str, message: str, detail=None):
        super().__init__(
            status_code=status_code,
            detail={"error": code, "message": message, "detail": detail},
        )
