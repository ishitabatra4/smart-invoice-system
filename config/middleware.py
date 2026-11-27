from django.db import OperationalError
from django.http import HttpResponse


class SecurityBoundaryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated or request.path.startswith("/accounts/"):
            response["Cache-Control"] = "private, no-store"
        return response

    def process_exception(self, request, exception):
        if isinstance(exception, OperationalError) and "locked" in str(exception).lower():
            response = HttpResponse("The record is busy. Please retry shortly.", status=409)
            response["Retry-After"] = "2"
            return response
        return None
