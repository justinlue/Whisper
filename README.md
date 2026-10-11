# Whisper

Instant messenger for a local network. Peer-to-peer messages and files, a
subnet-wide "Everyone" channel, and an incognito mode that is on by default.

## Requirements

Python 3.10+ with tkinter, and:

```
pip install -r requirements.txt
```

## Running

```
python -m whisper_im
```

or double-click `whisper.bat` (no console window).

The first time, Windows Firewall asks whether to allow Python on the network.
Allow it for **private** networks, otherwise nobody can reach you. Only one
Whisper can run per computer; a second launch reports that the port is taken.

## Using it

- **Peers** running Whisper on the same subnet appear in the list on the left
  by themselves. To reach someone else, type their IP into the box above the
  list and press *Add IP* (`192.168.1.20`, or `192.168.1.20:48765` for a
  non-default port).
- **Everyone** sends one message to every Whisper on the subnet. It does not
  cross routers or VLANs, is limited to 1000 bytes, and is **not encrypted**.
- **New group…** makes a group channel: give it a name and tick the people
  in it. Both are fixed from then on; nobody can rename the group or change
  who is in it. Each message goes to every member separately, encrypted like
  a one-to-one message. A message that missed someone says who, and `retry`
  sends it to them alone. Groups last until you close Whisper; if you come
  back, the next message from any member puts you in the group again.
- **@name** in a group or in Everyone highlights that person's name. Being
  mentioned yourself is highlighted more strongly, and if you are not looking
  at that conversation Whisper beeps, flashes its taskbar button, says who
  mentioned you in the status bar, and marks the conversation `(@ 2)`. Type
  the name as it is shown; capitals do not matter.
- **Send file…** works in a one-to-one conversation. The recipient is asked to
  accept each file. Your original is read in place; nothing is copied locally.
- A message that could not be delivered is marked `✗ not delivered` with a
  `retry` link. Nothing is queued.
- **Block** drops everything from that IP until you close Whisper.
- Enter sends, Shift+Enter starts a new line.
- **Emoji** work in messages and names: the *Emoji* button opens a picker that
  stays open until you close it or press Esc; `Win` + `.` (the Windows emoji
  panel) and paste work too. In the chat and the picker they are drawn in
  colour (this needs Pillow, from `requirements.txt`, and Windows' Segoe UI
  Emoji font; without them they are drawn in one colour). The box you type in
  and the list on the left still show them in one colour, and text copied
  out of the chat leaves the colour emoji behind. Combined emoji lose
  their combination: a skin-toned emoji is drawn in the default yellow, a
  family as its separate people, and a flag as its two letters.

## Incognito mode

Every launch starts in incognito, shown by the purple header.

| | Incognito (default) | Recording |
|---|---|---|
| Messages | Memory only, never written to disk | Appended, encrypted, to `data/history.whisper` |
| Received files | `files/incognito/`, deleted when Whisper closes | `files/`, kept |
| Header | Purple | Red |

Untick **Incognito** to start recording. Whisper asks for a numeric PIN of at
least 6 digits (twice the first time, to set it). Recording covers only what
happens from that moment; earlier messages stay unrecorded. Ticking the box
again stops recording and deletes nothing. The next launch is incognito again.

On close, Whisper warns if incognito files are about to be deleted, so you can
copy them out first (*Open files folder*). If Whisper is killed or crashes,
the leftovers are removed the next time it starts.

Things incognito does **not** do:

- It governs your disk only. The other side keeps whatever their own mode
  says; their mode is shown next to their name.
- Deletion is an ordinary delete, not a secure overwrite.
- A file you opened in another program may have been copied or cached by it.

## Security notes

- One-to-one messages and files are encrypted on the wire (ephemeral X25519
  per connection, AES-256-GCM). This stops someone sniffing the LAN. Peers are
  not authenticated, so it does not stop an active man-in-the-middle, and
  nicknames can be faked: trust the IP shown beside the name.
- Group messages travel the same encrypted way, one connection per member.
  A group is whatever its first message said it was: anyone can put you in a
  group, as anyone can message you, and only its members can speak in it.
- Presence and the Everyone channel are plaintext.
- The history file is AES-256-GCM under a key derived from the PIN with
  Argon2id (256 MiB). A numeric PIN is short: this keeps out someone browsing
  your disk, not a determined attacker who copies the file and brute-forces
  it. Use a long PIN.
- There is no history viewer yet. The file format is complete and decryptable
  (`HistoryLog.read_all` in `whisper_im/history.py`); the UI just never reads it.
- Forgot the PIN: choose *Forgot PIN…* in the PIN prompt. This deletes the
  saved history so a new PIN can be set.

## Data location

Everything lives next to the code: `files/`, `data/`, `config.json`. Set
`WHISPER_HOME` to move it.

`config.json` holds your display name, and optionally `port` (default 48765,
TCP and UDP) and `bind` (a local IP to listen on instead of all interfaces).

## Tests

```
python -m pytest
```
