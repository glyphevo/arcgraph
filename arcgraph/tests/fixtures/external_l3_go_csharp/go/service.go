package service

import "context"

type Saver interface {
	Save(context.Context, string) error
}

type Repository struct {
	store string
}

func NewRepository(store string) *Repository {
	return &Repository{store: store}
}

func (repo *Repository) Save(ctx context.Context, value string) error {
	return nil
}

func UseSaver(ctx context.Context, saver Saver) error {
	return saver.Save(ctx, "demo")
}
