from django.contrib.auth.decorators import login_required
from django.http import HttpResponse


def home(request):
    return HttpResponse("home")


@login_required
def secret(request):
    return HttpResponse("secret")
