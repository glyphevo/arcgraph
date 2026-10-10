"""Known inherited boundary methods retain their base evidence."""

import pytest

from arcgraph.tests.test_generator_and_dynamic_exports import _targets


@pytest.mark.parametrize(
    "prefix",
    ["import httpx", "import httpx2 as httpx", "from httpx import Client as Base"],
)
def test_known_external_base_method(tmp_path, prefix):
    base = "Base" if prefix.startswith("from") else "httpx.Client"
    owner = "httpx2.Client" if "httpx2" in prefix else "httpx.Client"
    targets = _targets(
        tmp_path,
        {
            "user": prefix
            + f"\nclass Client({base}): pass\ndef use(client: Client):\n    client.get('/')\n    client.post('/')\n"
        },
        v2=True,
    )
    assert {f"extsym:{owner}.get", f"extsym:{owner}.post"} <= targets


def test_finite_alternative_http_imports_retain_receiver_owner(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx2 as httpx\nelse:\n    try: import httpx2 as httpx\n    except ImportError: import httpx\nclass Client(httpx.Client): pass\ndef use(client: Client): client.get('/')\n"
        },
        v2=True,
    )
    assert "extsym:pkg.user.Client.get" in targets


@pytest.mark.parametrize(
    "prefix,base,member",
    [
        ("import mystery", "mystery.Client", "get"),
        ("import httpx\nhttpx=other()", "httpx.Client", "get"),
        ("import httpx", "httpx.Client", "made_up"),
        ("import httpx\nfrom unknown import Mix", "Mix, httpx.Client", "get"),
        (
            "if flag: import httpx\nelse: import unrelated as httpx",
            "httpx.Client",
            "get",
        ),
    ],
)
def test_unproved_external_base_is_not_guessed(tmp_path, prefix, base, member):
    targets = _targets(
        tmp_path,
        {
            "user": prefix
            + f"\nclass Client({base}): pass\ndef use(client: Client): client.{member}('/')\n"
        },
        v2=True,
    )
    assert not any(t.endswith("." + member) for t in targets)


def test_local_member_shadows_known_inherited_method(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "import httpx\nclass Client(httpx.Client):\n    get = None\ndef use(client: Client): client.get('/')\n"
        },
        v2=True,
    )
    assert "extsym:httpx.Client.get" not in targets


def test_local_member_shadows_finite_alternative_bases(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "try: import httpx2 as httpx\nexcept ImportError: import httpx\nclass Client(httpx.Client):\n    get=None\ndef use(client: Client): client.get('/')\n"
        },
        v2=True,
    )
    assert "extsym:pkg.user.Client.get" not in targets
