package src

type Handler struct{}

func (h Handler) Serve() {
    Helper()
    External()
    Missing()
}
