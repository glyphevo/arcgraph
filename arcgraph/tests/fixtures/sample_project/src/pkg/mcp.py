class ToolGroup:
    CORE = "core"


def register_tool(name: str, group: str):
    def decorator(func):
        return func

    return decorator


@register_tool(name="store_memory", group=ToolGroup.CORE)
async def store_memory_tool(content: str) -> str:
    return content
