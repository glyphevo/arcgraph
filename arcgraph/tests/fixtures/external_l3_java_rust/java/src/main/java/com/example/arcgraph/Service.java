package com.example.arcgraph;

import java.util.Objects;

@interface Tracked {}

interface Saver {
    String save(String value);
}

enum Mode {
    PRIMARY
}

record SaveRequest(String value) {}

@Tracked
class RepositoryService implements Saver {
    private final String name;

    RepositoryService(String name) {
        this.name = Objects.requireNonNull(name);
    }

    @Override
    public String save(String value) {
        return name + ":" + value;
    }

    public String run(SaveRequest request) {
        return save(request.value());
    }

    public Class<?> dynamic(String name) throws ClassNotFoundException {
        return Class.forName(name);
    }
}
