# CLAUDE.md

LAN instant messenger (tkinter) with a default-on incognito mode. See `README.md` for user-facing behaviour.

## Commands

```
python -m whisper_im          # run
python -m pytest              # all tests (~20 s; test_net.py uses real loopback sockets)
python -m pytest tests/test_history.py -q
```

Tests set `WHISPER_HOME` to a temp dir and `WHISPER_ARGON2_TIME/MEM` low (see `tests/conftest.py`); do the same in any ad-hoc script, or it writes into the repo and spends ~1 s per key derivation.

## Architecture

`whisper_im/`:

- `session.py` — incognito/recording state for one run. Decides whether an event is recorded and which directory a received file goes to.
- `history.py` — encrypted append-only log format. The app only appends; `read_all` exists for tests and a future viewer.
- `files.py` — peer-supplied filename sanitising, collision naming, incognito wipe, startup sweep.
- `wire.py` — TCP handshake + encrypted `Channel`, UDP datagram encoding. Protocol version lives here.
- `net.py` — `Node`: threads for accept, UDP receive, presence. Reports via `on_event(dict)` **from network threads**.
- `ui.py` — tkinter. Drains a queue of node events on a timer; never touch widgets from a `Node` callback.

## Invariants

- Incognito writes nothing about conversations to disk. Messages live in `Conversation.items` only; received files go to `files/incognito/`, which is emptied on quit and at startup.
- The mode in force when an event happens decides its fate; toggling never retroactively records or deletes.
- A file is written as `name.part` and renamed when complete, so `sweep()` can identify half-received files.
- `sweep()` runs only after the node has bound its port, so a refused second instance cannot delete the first one's files.
- Anything arriving from the network is untrusted: validate types and lengths in `net.py`, and pass filenames through `sanitize_filename`.
- A wire-format change needs a `VERSION` bump in `wire.py`; a history-format change needs a `FORMAT` bump in `history.py`.

## Testing

Non-UI modules are covered by pytest. `ui.py` has no automated tests; check it by running two instances on different machines, or one instance against a `Node` on loopback with `bind`/`port` set in `config.json`.
