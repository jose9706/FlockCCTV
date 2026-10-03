# Repository guidance for coding agents

- This is a Python Discord bot. Use `README.md` for setup and operation,
  `DESIGN.md` for behavior and measurement rules, and `IMPLEMENTATION.md` for
  component contracts. Read the relevant sections for the change at hand.
- Keep changes focused. Update the corresponding tests and documentation when
  behavior, commands, configuration, storage, permissions, or reports change.
- Preserve the bot's privacy and accuracy rules: do not store message bodies or
  turn gaps in observed voice activity into estimated time. Preserve configured
  guild/channel access, private status/control replies, and requester visibility
  checks for voice-channel reports. Treat data deletion, retention, backups, and
  recovery as related behavior.
- Keep real Discord tokens, filled-in `.env` files, production databases,
  backups, logs, and message contents out of commits and public output. Use
  synthetic values in tests.
- Run the full test suite after code changes (set up `.venv` from `README.md` if
  needed). The tests use local fixtures and do not require a Discord connection:

  ```sh
  PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
  ```
- Keep GitHub Actions limited to installing dependencies and running tests.
  Do not add artifact uploads, cache uploads, or deployment steps.
