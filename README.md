# Remote Bridge for Kodi

A webOS Homebrew Channel app that lets an LG TV's own remote (Magic Remote
included) control Kodi running on another box, over the network. It is the
TV side of the Remote Bridge in
[service.webostv](https://github.com/dangerouslaser/kodi-webos-control).

Needs a rooted TV with [Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel).
Tested on webOS 5.5 (CX), 9 (B4) and 10 (G5).

## How it works

- `src/`: the app. Kodi launches it with its address and a token; it writes
  the config, starts the daemon and switches back to Kodi. It also shows the
  installed and running versions, has a Restart button, and installs updates
  Kodi hands it.
- `daemon/bridge.py`: a small root daemon (Python 2 or 3), run as a transient
  systemd unit and re-created at boot by a Homebrew Channel startup hook
  (`daemon/97-kodiremote`). While Kodi is on screen it grabs the remote's input
  devices and streams buttons, pointer and wheel to Kodi (newline JSON over
  TCP, port 9779 by default); otherwise the TV has the remote. It also mirrors
  the TV's pointer settings and images to Kodi, tells it which Magic Remote is
  paired (model, number keys, mute key), and hands the pointer back to webOS
  where Kodi's pointer was.

## Install

You don't install this by hand: in Kodi, *Settings → LG TV → Remote Bridge →
Set up Remote Bridge*. The add-on carries the ipk and serves it to Homebrew
Channel as a one-package repository (port 9780 by default), so the TV installs
the exact version that add-on was built with, and updates from it too.

## Build

    ./build.sh          # build/com.github.dangerouslaser.kodiremote_<version>_all.ipk

`tools/pack_ipk.py` builds the ipk instead of `ares-package`: ares-package dates
every file 1970, and webOS 5's package verifier rejects that
("-5: ipk verified failed"). Builds use a fixed timestamp, so they're
reproducible. To bundle a build into the add-on, run its
`scripts/bundle_bridge.sh`.
