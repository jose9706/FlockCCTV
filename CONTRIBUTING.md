# Contributing

Thanks for helping improve Leland Tracker. Small fixes and focused features are
welcome. For a larger change, open an issue first so we can agree on its scope.

1. Fork the repository and open a pull request against `main`. Describe what
   changed and why. Keep unrelated changes in separate pull requests.
2. Add or update tests for behavior changes. Run the suite before submitting:

   ```sh
   PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
   ```

   Set up `.venv` using the instructions in [README.md](README.md) if needed.
3. Update the README or design notes when a change affects commands,
   configuration, stored data, permissions, or what the bot reports.
4. Bump `__version__` in `src/flock_cctv/__init__.py` when the change
   affects what runs on the bot: MAJOR for incompatible configuration or
   storage changes, MINOR for features, PATCH for fixes. Docs- and test-only
   changes need no bump.
5. Keep private data out of commits, issues, and pull requests. Do not post
   Discord tokens, filled-in `.env` files, production databases, backups, or
   message contents. Use made-up values in examples and tests.

Pull requests must pass the `tests` GitHub Actions check before merging. The
workflow only runs tests and does not upload artifacts.
