import Foundation

protocol Saver {
    func save(_ payload: String) -> String
}

struct Repository {
    let name: String

    func save(_ payload: String) -> String {
        "\(name):\(payload)"
    }
}

extension Repository: Saver {}

func useSaver(_ payload: String) -> String {
    Repository(name: "main").save(payload)
}
