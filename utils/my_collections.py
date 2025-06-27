from collections import UserDict


class Collection(UserDict):
    def find(self, fn=lambda value, key, collection: None, this_arg=None):
        if this_arg is not None:
            fn = fn.__get__(this_arg)
        for key, val in self.items():
            if fn(val, key, self):
                return val

    def set_options(self, name, options={}):
        if not name or not options:
            return name if not options else options
        if name not in self.data:
            return f'"{name}" is not matched any commands!'
        self.data[name]["options"].update(options)
        return self.data[name]