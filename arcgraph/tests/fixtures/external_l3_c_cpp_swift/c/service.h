#pragma once

typedef struct Repository {
    const char *name;
} Repository;

typedef Repository *RepositoryHandle;

typedef enum Status {
    STATUS_OK,
    STATUS_ERROR
} Status;

const char *repository_save(RepositoryHandle repository, const char *payload);
