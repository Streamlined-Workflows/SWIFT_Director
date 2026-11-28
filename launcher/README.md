# SWIFT Director Launcher

A small Windows program that opens SWIFT Director and the other places on the server that are reached over Tailscale.
It looks like SWIFT Launcher (title bar, a rail of tiles, a page per tile) but has no sign-in and no set-up steps.

- **Tabs.** One per place you can open in a browser: SWIFT Director, ComfyUI, and anything SWIFT Director lists later.
  Each tab says what the place is, shows whether it answers, and has an **Open** button (your default browser).
- **Sync with Tailscale.** Asks Tailscale on this PC for your devices, finds the one answering as SWIFT Director on
  port 8190, and reads its list of places and ports from `GET /api/launcher` (`launcher_manifest()` in `app.py`).
  Adding an entry there adds a tab here. If the server does not answer, the two usual ports (8190, 8188) are shown.
- **On the server.** The services that listen on the server only (song analysis, speech, the 3D converter, the
  language model), for information. They cannot be opened from the PC, on purpose.
- **Add a port.** A tab of your own for anything else on the server. Saved on this PC only.
- **Tailscale only.** It accepts only Tailscale addresses (100.64.0.0/10) and `.ts.net` names, because SWIFT Director
  and ComfyUI have no login. It sends nothing anywhere: the page itself can make no network requests, and the program
  only talks to the Tailscale program on this PC and to the chosen device.

## Build

```
cd launcher
npm install
npm run package:win        # writes release/SWIFT Director Launcher.exe (portable, about 110 MB, unsigned)
```

`npm run assets` (run by the scripts above) copies the colour tokens, fonts, wordmark and icon from `../static`, so
there is one source for them. `npm start` runs it without packaging. Settings are kept in
`%APPDATA%\swift-director-launcher\settings.json`.

Built on Linux, the program is not code-signed, so Windows SmartScreen shows a warning the first time it is run.
