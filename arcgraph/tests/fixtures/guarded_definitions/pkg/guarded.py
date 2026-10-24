def top_fn(x):
    return x


def make():
    if True:

        def nested_fn(x):
            return x * 2

        return nested_fn


if __name__ == "__main__":

    def guarded_fn(x):
        return x + 10
