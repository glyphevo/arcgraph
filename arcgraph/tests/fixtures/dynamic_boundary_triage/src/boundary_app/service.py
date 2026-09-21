def external_service_boundaries(
    http_client,
    http_session,
    redis_client,
    producer,
    broker,
    channel,
    cursor,
    db_connection,
) -> None:
    http_client.get("/items")
    http_session.post("/items", json={"name": "arc"})
    redis_client.publish("events", "payload")
    producer.send("events", b"payload")
    broker.publish("events", b"payload")
    channel.basic_publish(exchange="", routing_key="events", body=b"payload")
    cursor.fetchone()
    db_connection.execute("select 1")


def framework_magic_boundaries(router, app, celery_app, cli, handler) -> None:
    router.add_api_route("/dynamic", handler)
    app.include_router(router)
    celery_app.autodiscover_tasks(["boundary_app"])
    cli.add_command(handler)


def true_dynamic_boundaries(cache, bag, plugin) -> None:
    cache.get("content")
    bag.add("content")
    plugin.run("content")
