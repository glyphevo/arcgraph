from django.urls import include, path, re_path

from matrix_app import django_views

urlpatterns = [
    path("articles/", django_views.article_list, name="article-list"),
    re_path(
        r"^articles/(?P<pk>\\d+)/$", django_views.article_list, name="article-detail"
    ),
    path("nested/", include("matrix_app.nested_urls")),
]
