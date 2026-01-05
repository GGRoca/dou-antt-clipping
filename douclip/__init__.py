class Storage:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path

        # garante que a pasta existe (ex.: data/)
        db_dir = os.path.dirname(self.sqlite_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._init_db()
