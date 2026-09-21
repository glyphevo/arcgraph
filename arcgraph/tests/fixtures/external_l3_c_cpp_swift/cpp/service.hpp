#pragma once

#include <string>

namespace arcgraph {

struct SaveRequest {
    std::string payload;
};

class BaseSaver {
public:
    virtual std::string save(const SaveRequest &request) const = 0;
};

class RepositoryService : public BaseSaver {
public:
    explicit RepositoryService(std::string name);
    ~RepositoryService();
    std::string save(const SaveRequest &request) const override;
    std::string run(const SaveRequest &request) const;

private:
    std::string name_;
};

template <typename T>
struct Box {
    T value;
};

}  // namespace arcgraph
