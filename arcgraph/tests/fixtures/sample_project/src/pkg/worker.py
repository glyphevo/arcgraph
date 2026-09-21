from arq import cron


async def generate_embedding_task(ctx: dict[str, object], memory_id: str) -> None:
    ctx["memory_id"] = memory_id


async def cleanup_task(ctx: dict[str, object]) -> None:
    ctx.clear()


async def schedule_embedding(redis: object, memory_id: str) -> None:
    await redis.enqueue_job(
        "generate_embedding_task",
        memory_id,
        _queue_name="arq:embedding",
    )


class WorkerSettings:
    functions = [generate_embedding_task]
    cron_jobs = [cron(cleanup_task)]
    queue_name = "arq:embedding"
