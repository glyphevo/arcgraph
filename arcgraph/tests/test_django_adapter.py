"""Tests for Django adapter — URL routes, models, admin, ORM edges."""

from __future__ import annotations

import ast

from arcgraph.adapters.django_adapter import DjangoAdapter
from arcgraph.core.schemas import FileRecord, Node


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _file(module: str = "myapp.urls", path: str = "myapp/urls.py") -> FileRecord:
    return FileRecord(
        module=module,
        path=path,
        is_package=False,
        abs_path=path,
        source_root=".",
        file_hash="test",
        line_count=0,
    )


def _nodes(*names: str) -> list[Node]:
    """Create minimal function nodes for resolution."""
    return [
        Node(
            id=f"fn:{name}",
            kind="function",
            name=name.rsplit(".", 1)[-1],
            qualname=name,
        )
        for name in names
    ]


# ── detect ──────────────────────────────────────────────────


def test_detect_true_with_django_import() -> None:
    adapter = DjangoAdapter()
    source = "from django.urls import path\n"
    fr = _file()
    tree = _parse(source)
    assert adapter.detect([fr], {fr.path: tree}, [], set()) is True


def test_detect_false_without_django() -> None:
    adapter = DjangoAdapter()
    source = "import os\n"
    fr = _file()
    tree = _parse(source)
    assert adapter.detect([fr], {fr.path: tree}, [], set()) is False


# ── URL patterns ────────────────────────────────────────────


def test_urlpatterns_basic() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.urls import path
from myapp import views

urlpatterns = [
    path("users/", views.user_list, name="user-list"),
    path("users/<int:pk>/", views.user_detail, name="user-detail"),
]
"""
    fr = _file()
    tree = _parse(source)
    nodes = _nodes("myapp.views.user_list", "myapp.views.user_detail")
    analysis = adapter.analyze([fr], {fr.path: tree}, nodes, {"myapp", "myapp.views"})

    route_nodes = [n for n in analysis.nodes if n.kind == "route"]
    assert len(route_nodes) == 2
    paths = sorted(n.properties["path"] for n in route_nodes)
    assert "/users/" in paths
    assert "/users/<int:pk>/" in paths

    invokes = [e for e in analysis.edges if e.kind == "invokes"]
    assert len(invokes) == 2


def test_urlpatterns_include_emits_edge() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.urls import path, include

urlpatterns = [
    path("api/", include("myapp.api.urls")),
]
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], {"myapp.api.urls"})

    inc_edges = [e for e in analysis.edges if e.kind == "includes_router"]
    assert len(inc_edges) == 1
    assert inc_edges[0].target == "mod:myapp.api.urls"
    assert inc_edges[0].resolution.status == "resolved"


def test_urlpatterns_include_materializes_an_unresolved_module_reference() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.urls import path, include

urlpatterns = [
    path("api/", include("external_app.urls")),
]
"""
    fr = _file()
    tree = _parse(source)

    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    edge = next(edge for edge in analysis.edges if edge.kind == "includes_router")
    placeholder = next(
        node for node in analysis.nodes if node.id == "mod:external_app.urls"
    )
    assert edge.confidence == "unresolved"
    assert edge.resolution.status == "unresolved"
    assert placeholder.properties["external_reference"] is True


def test_repeated_unresolved_include_never_self_promotes_to_resolved() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.urls import include, path

urlpatterns = [
    path("api/", include("external_app.urls")),
    path("admin/", include("external_app.urls")),
]
"""
    fr = _file()

    analysis = adapter.analyze([fr], {fr.path: _parse(source)}, [], set())

    edges = [edge for edge in analysis.edges if edge.kind == "includes_router"]
    placeholders = [
        node for node in analysis.nodes if node.id == "mod:external_app.urls"
    ]
    assert len(edges) == 2
    assert len(placeholders) == 1
    assert {edge.confidence for edge in edges} == {"unresolved"}
    assert {edge.resolution.status for edge in edges} == {"unresolved"}


def test_urlpatterns_cbv_as_view() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.urls import path
from myapp.views import UserView

urlpatterns = [
    path("users/", UserView.as_view(), name="user-view"),
]
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    route_nodes = [n for n in analysis.nodes if n.kind == "route"]
    assert len(route_nodes) == 1

    invokes = [e for e in analysis.edges if e.kind == "invokes"]
    assert len(invokes) == 1
    # Target should be the class, not a function
    assert invokes[0].target.startswith("class:")


# ── models ──────────────────────────────────────────────────


def test_model_extraction() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.db import models

class Article(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "blog_articles"
"""
    fr = _file("myapp.models", "myapp/models.py")
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    table_nodes = [n for n in analysis.nodes if n.kind == "table"]
    assert len(table_nodes) == 1
    assert table_nodes[0].name == "blog_articles"
    assert table_nodes[0].properties["backend"] == "django_orm"
    assert set(table_nodes[0].properties["fields"]) == {"title", "body", "created_at"}

    maps_to = [e for e in analysis.edges if e.kind == "maps_to"]
    assert len(maps_to) == 1
    assert maps_to[0].source == "class:myapp.models.Article"


def test_model_default_table_name() -> None:
    adapter = DjangoAdapter()
    source = """\
from django.db import models

class User(models.Model):
    name = models.CharField(max_length=100)
"""
    fr = _file("myapp.models", "myapp/models.py")
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    table_nodes = [n for n in analysis.nodes if n.kind == "table"]
    assert len(table_nodes) == 1
    # Default: app_label + "_" + lowercased class name (Django convention)
    assert table_nodes[0].name == "myapp_user"


# ── admin ───────────────────────────────────────────────────


def test_admin_register_decorator() -> None:
    adapter = DjangoAdapter()
    model_source = """\
from django.db import models

class Article(models.Model):
    title = models.CharField(max_length=200)
"""
    admin_source = """\
from django.contrib import admin
from myapp.models import Article

@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title",)
"""
    fr_model = _file("myapp.models", "myapp/models.py")
    fr_admin = _file("myapp.admin", "myapp/admin.py")
    tree_model = _parse(model_source)
    tree_admin = _parse(admin_source)

    analysis = adapter.analyze(
        [fr_model, fr_admin],
        {fr_model.path: tree_model, fr_admin.path: tree_admin},
        [],
        set(),
    )

    reads = [e for e in analysis.edges if e.kind == "reads"]
    assert len(reads) == 1
    assert reads[0].source == "class:myapp.admin.ArticleAdmin"
    assert reads[0].target == "table:myapp_article"


def test_admin_site_register() -> None:
    adapter = DjangoAdapter()
    model_source = """\
from django.db import models

class Tag(models.Model):
    name = models.CharField(max_length=50)
"""
    admin_source = """\
from django.contrib import admin
from myapp.models import Tag

admin.site.register(Tag)
"""
    fr_model = _file("myapp.models", "myapp/models.py")
    fr_admin = _file("myapp.admin", "myapp/admin.py")
    tree_model = _parse(model_source)
    tree_admin = _parse(admin_source)

    analysis = adapter.analyze(
        [fr_model, fr_admin],
        {fr_model.path: tree_model, fr_admin.path: tree_admin},
        [],
        set(),
    )

    reads = [e for e in analysis.edges if e.kind == "reads"]
    assert len(reads) == 1
    assert reads[0].target == "table:myapp_tag"


# ── ORM queries ─────────────────────────────────────────────


def test_orm_read_write_edges() -> None:
    adapter = DjangoAdapter()
    model_source = """\
from django.db import models

class Post(models.Model):
    title = models.CharField(max_length=200)
"""
    view_source = """\
from django.shortcuts import render
from myapp.models import Post

def list_posts(request):
    posts = Post.objects.filter(published=True)
    return render(request, "list.html", {"posts": posts})

def create_post(request):
    Post.objects.create(title=request.POST["title"])
"""
    fr_model = _file("myapp.models", "myapp/models.py")
    fr_view = _file("myapp.views", "myapp/views.py")
    tree_model = _parse(model_source)
    tree_view = _parse(view_source)

    analysis = adapter.analyze(
        [fr_model, fr_view],
        {fr_model.path: tree_model, fr_view.path: tree_view},
        [],
        set(),
    )

    reads = [e for e in analysis.edges if e.kind == "reads"]
    writes = [e for e in analysis.edges if e.kind == "writes"]

    assert len(reads) >= 1
    assert any(e.target == "table:myapp_post" for e in reads)
    assert len(writes) >= 1
    assert any(e.target == "table:myapp_post" for e in writes)


def test_orm_edge_inside_class_method_uses_method_id() -> None:
    """ORM edges inside class methods must use method: prefix, not fn:."""
    adapter = DjangoAdapter()
    model_source = """\
from django.db import models

class Article(models.Model):
    title = models.CharField(max_length=200)
"""
    view_source = """\
from myapp.models import Article

class ArticleService:
    def get_published(self):
        return Article.objects.filter(published=True)

    def publish(self, pk):
        Article.objects.update(pk=pk, published=True)
"""
    fr_model = _file("myapp.models", "myapp/models.py")
    fr_service = _file("myapp.services", "myapp/services.py")
    tree_model = _parse(model_source)
    tree_service = _parse(view_source)

    analysis = adapter.analyze(
        [fr_model, fr_service],
        {fr_model.path: tree_model, fr_service.path: tree_service},
        [],
        set(),
    )

    orm_edges = [e for e in analysis.edges if e.kind in ("reads", "writes")]
    sources = {e.source for e in orm_edges}
    # Must be method: prefix, not fn:
    assert all(
        s.startswith("method:") for s in sources
    ), f"Expected method: prefix for class method sources, got {sources}"
    assert "method:myapp.services.ArticleService.get_published" in sources
    assert "method:myapp.services.ArticleService.publish" in sources


def test_cross_app_same_model_name_resolves_via_import() -> None:
    """Same model name in different apps: FQN import resolution picks the right table."""
    adapter = DjangoAdapter()
    app1_source = """\
from django.db import models

class User(models.Model):
    name = models.CharField(max_length=100)
"""
    app2_source = """\
from django.db import models

class User(models.Model):
    email = models.EmailField()
"""
    view_source = """\
from app1.models import User

def list_users(request):
    return User.objects.all()
"""
    fr1 = _file("app1.models", "app1/models.py")
    fr2 = _file("app2.models", "app2/models.py")
    fr_view = _file("app1.views", "app1/views.py")
    tree1 = _parse(app1_source)
    tree2 = _parse(app2_source)
    tree_view = _parse(view_source)

    analysis = adapter.analyze(
        [fr1, fr2, fr_view],
        {fr1.path: tree1, fr2.path: tree2, fr_view.path: tree_view},
        [],
        {"app1.models"},
    )

    # Both table nodes should be created (they have distinct IDs)
    table_nodes = [n for n in analysis.nodes if n.kind == "table"]
    table_ids = {n.id for n in table_nodes}
    assert "table:app1_user" in table_ids
    assert "table:app2_user" in table_ids

    # ORM edge should resolve via import to the correct app1 table
    orm_edges = [e for e in analysis.edges if e.kind in ("reads", "writes")]
    assert (
        len(orm_edges) == 1
    ), f"Expected 1 ORM edge via FQN resolution, got {orm_edges}"
    assert orm_edges[0].target == "table:app1_user"


def test_cross_app_same_model_name_no_edge_without_import() -> None:
    """Same model name, no import to disambiguate — no ORM edge produced."""
    adapter = DjangoAdapter()
    app1_source = """\
from django.db import models

class User(models.Model):
    name = models.CharField(max_length=100)
"""
    app2_source = """\
from django.db import models

class User(models.Model):
    email = models.EmailField()
"""
    # View uses User without any import from a models module
    view_source = """\
def list_users(request):
    return User.objects.all()
"""
    fr1 = _file("app1.models", "app1/models.py")
    fr2 = _file("app2.models", "app2/models.py")
    fr_view = _file("app1.views", "app1/views.py")
    tree1 = _parse(app1_source)
    tree2 = _parse(app2_source)
    tree_view = _parse(view_source)

    analysis = adapter.analyze(
        [fr1, fr2, fr_view],
        {fr1.path: tree1, fr2.path: tree2, fr_view.path: tree_view},
        [],
        set(),
    )

    # Simple name "User" is ambiguous and unresolvable — no ORM edge
    orm_edges = [e for e in analysis.edges if e.kind in ("reads", "writes")]
    assert (
        len(orm_edges) == 0
    ), f"Expected no ORM edges for ambiguous unresolvable model, got {orm_edges}"
