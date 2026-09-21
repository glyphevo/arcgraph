from django.db import models


class Article(models.Model):
    title = models.CharField(max_length=200)
    published = models.BooleanField(default=False)

    class Meta:
        db_table = "matrix_articles"
