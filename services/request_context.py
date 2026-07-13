from contextvars import ContextVar


_account_id = ContextVar("account_id", default="local")
_guest_mode = ContextVar("guest_mode", default=False)


def set_request_identity(account_id="local", guest=False):
    _account_id.set(str(account_id or "local"))
    _guest_mode.set(bool(guest))


def current_account_id():
    return _account_id.get()


def is_guest_mode():
    return _guest_mode.get()
