from arq.cron import cron

QUEUE_NAME = "arq:matrix"


async def process_job(ctx, payload: str) -> str:
    return payload


async def cleanup(ctx) -> None:
    return None


class WorkerSettings:
    queue_name = QUEUE_NAME
    functions = [process_job]
    cron_jobs = [cron(cleanup)]


async def enqueue(redis, payload: str) -> None:
    await redis.enqueue_job("process_job", payload, _queue_name=QUEUE_NAME)


async def enqueue_dynamic(redis, task_name: str, payload: str) -> None:
    await redis.enqueue_job(task_name, payload, _queue_name=QUEUE_NAME)
