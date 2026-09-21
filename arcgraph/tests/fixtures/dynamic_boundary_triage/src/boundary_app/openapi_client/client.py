def reflection_boundaries(client, method_name: str, module_name: str) -> None:
    getattr(client, method_name)("payload")
    __import__(module_name)
