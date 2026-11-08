class ChatFailure(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def invalid():
    return ChatFailure(400, "invalid_request", "Invalid message or format.")


def unauthenticated():
    return ChatFailure(401, "unauthenticated", "Invalid or expired session.")


def dependency():
    return ChatFailure(502, "dependency_unavailable",
                       "The service is temporarily unavailable.")
