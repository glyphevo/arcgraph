class Cursor:
    def fetchone(self) -> object:
        return object()

    def fetchall(self) -> list[object]:
        return []


class Connection:
    def execute(self, query: str) -> Cursor:
        return Cursor()


class StoreReader:
    @classmethod
    def from_current(cls, path: str) -> "StoreReader":
        return cls(path)

    def __init__(self, path: str) -> None:
        self.path = path

    def connect(self) -> Connection:
        return Connection()


def load_external_count(conn) -> object:
    return conn.execute("select count(*)").fetchone()
