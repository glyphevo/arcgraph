from django.contrib import admin

from matrix_app.django_models import Article


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title",)
