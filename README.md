# FairDrop

> Drop files to anyone on your Wi-Fi, from any browser. No app, no account, no cable.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

FairDrop is AirDrop for a whole network, served from one Python file. Start it on any
machine and open the printed address on a phone, laptop or tablet on the same Wi-Fi.
Everyone who opens the page appears on a live radar with a name of their own — tap
someone to send them files, and they choose whether to accept.

It works between platforms that normally can't share anything: iPhone to Windows,
Android to Mac, Linux to whatever is on the desk. Nobody installs anything.

---

## Quick start

```bash
python fairdrop.py
```

That's the whole setup. The console prints where to go:

```
  FairDrop - drop files to anyone on this network
  ----------------------------------------------
  On this computer   http://localhost:5000
  On the network     http://192.168.1.24:5000
  ----------------------------------------------
  Press Ctrl+C to stop.
```

Open the **network** address on every device you want to share between. Each open tab
becomes a person on the radar, visible to everyone else.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `5000` | Port to listen on |
| `--host` | `0.0.0.0` | Interface to bind; use `127.0.0.1` to keep it to this machine |

```bash
python fairdrop.py --port 8080
```

## Using it

- **Send** — click a person to pick files, or drag files straight onto them. Multiple
  files at a time are fine (up to 100 per transfer, no size limit).
- **Receive** — a request appears showing who it's from, the file list and the total
  size. Accept and it saves; a single file downloads as itself, several arrive as a zip.
  Decline and the data is deleted immediately.
- **Watch it go** — a progress ring sweeps around that person while the transfer runs,
  and the label under them reads `Waiting for accept…` → `Transferring…` → `Sent`.
- **Cancel** — click someone mid-send to withdraw the offer before they answer.
- **Rename yourself** — you start as something like `Solar Puffin`. Click the name in
  the footer to change it; the new one is remembered on that device.
- **Keyboard** — <kbd>Enter</kbd> accepts an incoming request, <kbd>Esc</kbd> declines,
  and everything is reachable by <kbd>Tab</kbd>.
- **Theme** — the toggle in the top right cycles auto → light → dark.

## How it works

Everything runs through the one server; there is no WebRTC and no peer-to-peer
negotiation, so both sides only ever need to reach this machine.

- **Presence** — each open page holds a Server-Sent Events stream. Joining or leaving
  re-broadcasts the roster, so the radar updates for everyone within a moment. Close the
  tab and you disappear. A ping every 15s keeps the stream alive; a peer that goes quiet
  for 40s is dropped.
- **Identity** — names are generated adjective + animal and kept unique among everyone
  currently connected. Your device type (phone, tablet, desktop) and browser are read
  from the user agent and shown under your name.
- **Transfers** — the sender creates an offer, which notifies the recipient immediately
  while the file bodies stream up in the background. If the recipient accepts before the
  upload finishes, their download simply waits for the data to arrive.
- **Storage** — files live in a temp directory only between send and accept. They are
  deleted when the transfer is answered or cancelled, when it expires after an hour, and
  when the server stops.

### HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/events?id=&name=` | SSE stream: `hello`, `peers`, `offer`, `response`, `file-ready`, `cancelled`, `ping` |
| `POST` | `/api/rename` | Change your display name |
| `POST` | `/api/transfers` | Create an offer and notify the recipient |
| `PUT` | `/api/transfers/<id>/files/<n>` | Upload one file body (raw, streamed) |
| `POST` | `/api/transfers/<id>/respond` | Accept or decline |
| `POST` | `/api/transfers/<id>/cancel` | Sender withdraws |
| `GET` | `/api/transfers/<id>/files/<n>/download` | Recipient saves one file |
| `GET` | `/api/transfers/<id>/zip` | Recipient saves everything |

Downloads are checked against the recipient's id and refused unless the transfer has
been accepted.

## Project layout

```
fairdrop.py        the entire server: presence, transfers, spooling, static files
web/
  index.html       markup and the SVG icon sprite
  style.css        the whole visual system, light and dark
  app.js           radar canvas, peers, transfers, dialogs
  fonts/           Open Sans (SIL Open Font License, see OFL.txt)
```

## Design

The interface is modelled on [PairDrop](https://github.com/schlagmichdoch/PairDrop),
itself a fork of Snapdrop — the same spatial idea, rebuilt against this server's API:

- **Radar** — eight concentric rings drift outward from the footer logo on a two-second
  loop, drawn on a full-page canvas (origin at the footer, ring spacing
  `max(0.6w, h) / 10`, opacity falling off with radius).
- **People as pucks** — each peer is a gradient circle carrying a device glyph, a
  colored highlight bar, their name and their device. New arrivals pop in.
- **Status on the person** — transfer state lives on the peer itself, as a sweeping
  progress ring and a changing sub-label. There is no separate transfer list.
- **Dialogs** — rounded cards with a blue title bar and uppercase letter-spaced buttons,
  growing from the center.

Accessibility is part of the build, not a pass afterwards: 44px minimum hit targets,
full keyboard operation, an `aria-live` region announcing transfer state, a real theme
toggle alongside the system preference, and animations that collapse under
`prefers-reduced-motion`.

## Security

FairDrop trusts its network. Anyone who can reach the port can appear on the radar and
send you a request — that's the point on a home or office Wi-Fi, and the accept step is
what keeps it polite rather than chaotic. Do not expose the port to the open internet.

Within that model: transfer ids are unguessable, downloads are tied to the recipient and
require an accepted transfer, and spooled data is deleted as soon as the transfer is
answered.

## Troubleshooting

**Other devices can't reach the address.** The first run usually triggers a firewall
prompt — allow Python on private networks. Also check that the devices are on the same
network and that the Wi-Fi doesn't have client isolation (common on guest networks).

**`Cannot listen on port 5000`.** FairDrop is already running, or something else has the
port. Stop the other instance or use `--port 5001`.

**The page loads but nobody appears.** Each person needs the page open — peers only
exist while a tab is. Different tabs on the same machine count as different people,
which is handy for testing.

## Credits

The design language comes from [PairDrop](https://github.com/schlagmichdoch/PairDrop)
(GPL-3.0) by schlagmichdoch, and the radar is a reimplementation of its canvas
animation. Open Sans in `web/fonts/` ships under the SIL Open Font License; its full
text is in `web/fonts/OFL.txt`.

Because the interface derives from a GPL-3.0 project, GPL-3.0 is the safe license for
this repository — add a `LICENSE` file before publishing.
