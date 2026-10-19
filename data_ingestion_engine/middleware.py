# app/api/middleware.py
import threading

_thread_locals = threading.local()


def get_current_request():
    """Anywhere in your code, call this to get the current Django request."""
    return getattr(_thread_locals, "request", None)


def get_current_token():
    """Grab the raw Bearer token from the current request, or None."""
    req = get_current_request()
    print(f"Current request: {req}")
    if not req:
        return None
    auth_header = req.META.get("HTTP_AUTHORIZATION", "")
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


class ThreadLocalRequestMiddleware:
    """Middleware that saves the request into thread-local storage."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.request = request
        response = self.get_response(request)
        return response
