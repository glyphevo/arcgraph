from matrix_app.django_models import Article


def article_list(request):
    return Article.objects.filter(published=True)


def article_create(request):
    Article.objects.create(title=request.POST["title"])
