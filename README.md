# camctl Studio

**An AI camera operator for people who shoot alone.**

camctl Studio sets up and operates your camera the way a professional photographer would, so you can concentrate on
what is in front of it: talking-head video, shorts, photos and video calls. Studio (with Claude, if you add your key)
looks at the picture, measures the light, sets exposure, focus, white balance and the file format, and then **locks**
everything. After that nothing changes by itself; you decide when something changes.

**Status: useful, and early.** Tested on one camera. Photos, focus, exposure setup, saved setups and focus stacking
work on that camera; video recording from the app is being verified after the latest fix.

## Tested with

- **Sony ZV-E10, firmware 2.03, on a Windows laptop.** Nothing else is tested yet.
- Other Sony cameras are **untested**. "Check this camera" (All settings › Studio) tests, without taking a photo, what
  works on yours and what to change.
- Other brands are **not supported yet**. Studio says so plainly when one is connected.

## Help add your camera

If you have a camera that is not in the table below, you can help, in about two minutes:

1. Connect your camera to Studio (USB or Wi-Fi).
2. Open All settings › Studio › **Check this camera** and press it. No photo is taken; Studio changes a few settings to
   a neighbouring value and back, and puts everything back as it was.
3. Press **Share**. Your browser opens a prefilled GitHub issue; read it, drag the report file onto the page, and press
   Submit yourself. Nothing is sent without your press.

The report contains: the app version, the camera's brand, model and firmware, the connection type, which functions
worked, the camera's own list of settings and allowed values, and timings. Studio removes the serial number, any names
you gave the camera, network details, paths and pictures before you see it. Every shared report is read, and a camera
is added to this table when it works.

## Tested cameras

| Camera | Firmware | Connection | Status | Since |
|---|---|---|---|---|
| Sony ZV-E10 | 2.03 | USB and Wi-Fi | tested by the author | November 2026 |

## What you need

- A Windows laptop with Python 3.12 (from python.org).
- The camera, and either Wi-Fi (no driver needed) or a USB cable.
- Your own Claude key from console.anthropic.com (optional: without it Studio uses its own built-in setup and the
  Claude features stay off). Studio asks for it in the app; it is never part of this project.

## Install

1. Download this project (green "Code" button › Download ZIP) and unpack it.
2. Install Python 3.12 from python.org (the default "Install for me" option).
3. Double-click **"Install or repair camctl Studio.cmd"**. It installs exactly what `requirements.txt` lists, for your
   Windows user only, from pypi.org. No admin rights, no system changes; it ends with "DONE".
4. Set up the camera as Studio's guide shows when you connect it. For the ZV-E10 over Wi-Fi: MENU › Network ›
   PC Remote Function › PC Remote: On, connection method Access Point; the first time choose Pairing on the camera.
5. Start Studio: double-click `tools\studio_app.pyw`, or run `start_studio.ps1`. Studio opens in its own window.

USB note: on Windows the camera's USB connection may need a driver that Studio never installs itself. Wi-Fi needs none.

## Recommended setup

1. A dummy battery / mains adapter in the battery slot (for the ZV-E10 the AC-PW20 type): live view drained about half
   the battery per hour, and the camera cannot run from USB without a battery.
2. The USB cable instead of Wi-Fi when you can: about 25 frames per second of live view instead of 12-17, and photos
   arrive faster.
3. A tripod: saved setups and "Line up" need the camera to stay in place.
4. One soft light at about 45 degrees to your face: much lower ISO (less noise) indoors.
5. A variable ND filter only outdoors or in bright light, with "ND filter on the lens" switched on in Studio: indoors it
   forces a very high ISO.

## iPad as a second screen

Run `start_studio.ps1 -Lan` on the laptop. The first start asks you to choose a PIN. Allow Studio on your *private*
home network only, for example with this one line in an administrator PowerShell (your choice, Studio never changes
firewall settings itself):

    New-NetFirewallRule -DisplayName "camctl Studio iPad" -Direction Inbound -Protocol TCP -LocalPort 8770 -Profile Private -RemoteAddress LocalSubnet -Action Allow

Then open the address Studio shows (All settings › Studio) on the iPad and enter the PIN. The Claude key can only be
entered on the laptop. Starting Studio normally again makes it laptop-only again.

## What Studio never does

- It never takes a photo or starts a recording without your press.
- It never changes a setting by itself after it locked; suggestions wait for your tap.
- It never sends anything in the background: Claude is only asked when you press a Claude button, and a camera report
  is only shared if you submit it yourself.
- It never installs drivers, never changes firewall or network settings, never deletes anything on the card.
- Your Claude key is stored encrypted for your Windows user, never shown again and never logged.

## For developers

`python -m pytest tests` runs the tests on a simulated ZV-E10 (no camera needed). `tools/sim_studio.py` starts Studio
on the simulator. `tools/publish.py --check` makes sure nothing personal would be published. A second camera brand
plugs in at `camctl/drivers` (see `drivers/base.py` for the functions Studio needs). See CONTRIBUTING.md.

## Licence

MIT, see [LICENSE](LICENSE).
