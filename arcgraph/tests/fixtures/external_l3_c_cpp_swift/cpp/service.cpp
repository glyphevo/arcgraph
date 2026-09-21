#include "service.hpp"

namespace arcgraph {

RepositoryService::RepositoryService(std::string name) : name_(std::move(name)) {}

RepositoryService::~RepositoryService() = default;

std::string RepositoryService::save(const SaveRequest &request) const {
    return name_ + ":" + request.payload;
}

std::string RepositoryService::run(const SaveRequest &request) const {
    return save(request);
}

}  // namespace arcgraph
