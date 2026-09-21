#include "service.h"

#define SERVICE_PREFIX "repo"

Repository default_repository = {"main"};

const char *repository_save(RepositoryHandle repository, const char *payload) {
    (void)repository;
    return payload;
}

const char *use_repository(const char *payload) {
    return repository_save(&default_repository, payload);
}
