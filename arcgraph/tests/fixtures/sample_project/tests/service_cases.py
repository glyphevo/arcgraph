from pkg.service import Greeter, build_message


def test_build_message() -> None:
    greeter = Greeter(prefix="hi")
    assert build_message(greeter.greet("Ada")) == "HI, ADA"
