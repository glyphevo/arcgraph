from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", RedirectView.as_view(url="/home/")),
    path("admin/", admin.site.urls),
    path("home/", views.home),
    path("secret/", login_required(views.secret)),
]
