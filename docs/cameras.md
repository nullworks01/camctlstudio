# Cameras and how Studio talks to them

camctl Studio was built and tested on one camera: a **Sony ZV-E10 (firmware 2.03)**. Everything below that is marked
*untested* was written from public documentation and tested only against simulated cameras. If you own one of these
cameras, "Check this camera" (All settings › Studio) tests it without taking a photo, and **Share** sends the report
only when you press Submit on the issue page yourself.

## Routes

| Route | Cameras | What works | Status | Ships with Studio |
|---|---|---|---|---|
| Sony PTP | Sony Alpha / ZV / FX with PC Remote | everything in Studio, USB and Wi-Fi | tested on a ZV-E10; other models untested | yes |
| Canon EOS PTP | Canon EOS DSLRs and EOS R (USB) | settings, live picture, photo (RAW + JPEG to the PC), video start / stop, focus steps, tap to focus | **untested** | yes |
| Nikon PTP | Nikon DSLRs and Z (USB) | settings, live picture, photo to the PC, video start / stop, focus steps, tap to focus | **untested** | yes |
| Plain PTP | Fujifilm, Panasonic, OM System / Olympus, Ricoh / Pentax, Leica (USB) | the settings the camera lists as standard PTP, the shutter, the files; **no live picture** | **untested** | yes |
| Canon / Nikon PTP/IP | Canon EOS and Nikon with Wi-Fi remote control (same drivers as on USB, over the network, port 15740) | as on USB | **untested** | yes |
| Fujifilm Wi-Fi | Fujifilm X / GFX with PC Shoot over Wi-Fi (ports 55740-55742) | standard PTP settings, shutter, live picture, video start / stop | **untested** | yes |
| Panasonic Wi-Fi | LUMIX with Remote Shooting (HTTP on the camera's own network, 192.168.54.1) | shutter speed, aperture, ISO, battery, live picture, photo (stays on the card), video, power zoom, focus, tap to focus | **untested** | yes |
| OM System / Olympus Wi-Fi | OM-D / PEN / TG with OI.Share remote (HTTP, 192.168.0.10) | shutter, aperture, ISO, exposure compensation, mode, live picture, photo (RAW + JPEG to the PC), tap to focus | **untested** | yes |
| Ricoh / Pentax Wi-Fi | GR II / III / IIIx, Pentax K-1 / KP / K-3 III (JSON over HTTP, 192.168.0.1) | aperture, shutter, ISO, compensation, white balance, battery, live picture, photo (RAW + JPEG to the PC), video, tap to focus | **untested** | yes |
| gphoto2 | the cameras libgphoto2 supports | settings, live picture, photo | **untested**, Linux / macOS only | no: only if the `gphoto2` Python package is already installed, and `CAMCTL_ROUTE=gphoto2` is set |
| Maker SDK | Sony Camera Remote SDK, Canon EDSDK, Nikon SDK | whatever the adapter provides | plug-in point only | no |

Not supported: GoPro and Blackmagic cameras (they are controlled through their own apps / APIs, not PTP), Canon
PowerShots without EOS remote mode, Leica over Wi-Fi, Canon's CCAPI (needs a key from Canon).

Wi-Fi: on the **Your camera** start screen, or All settings › Studio › Connection › Wi-Fi. Pick the camera maker, then Find camera (it looks for every maker
above: SSDP, the cameras' own network addresses, and the PTP/IP and Fujifilm ports on your network) or type the
address. Sony is the default, and a Sony works exactly as before.

## The live picture, per camera

Studio picks the picture source by itself, best first, and says on screen where the picture comes from or why there
is none:

1. **The camera's own live view over the control cable** (Sony, Canon EOS, Nikon): in step with every setting.
2. **The camera's webcam mode** (Fujifilm X Webcam / USB Webcam, LUMIX Webcam, OM System Webcam, EOS Webcam Utility,
   Nikon Webcam Utility, Sony USB Streaming): a video device named after the maker. It is the connected camera itself,
   so Studio uses it by itself when the camera gives no live view over PTP. On many bodies webcam mode and remote
   control are different USB modes, so one cable gives one of them; a second connection (or an HDMI card) gives both.
3. **An HDMI capture card** (Cam Link and similar) on the camera's clean HDMI output: works with every camera that has
   one. Studio never opens a capture card by itself; it offers it, and you pick it in More › Capture card.

Studio never opens the laptop's own webcam.

## Models Studio recognises

`camctl/drivers/usb_models.json` holds libgphoto2's list of 789 camera models by USB id (names and ids only), with
whether each USB mode allows remote control and a live picture. Studio uses it to name a camera before it connects
and to say plainly when a camera is in a USB mode that only copies files (for example a Sony in MTP mode instead of
PC Remote). It lists 51 remote-controllable Sony models (ZV-E10, ZV-1, a6100 / a6400 / a6600 / a6700, a7 III /
IV, a7C / a7C II, a7R III-V, a7S III, a1, a9 III, FX3 and others), all driven by the same Sony driver as the ZV-E10.
Recognised is not tested: every model but the ZV-E10 is untested.

## What stays on the camera

- **Canon**: the mode dial and the AF / MF switch on the lens. Set the dial to M.
- **Nikon**: the exposure mode on bodies with a mode dial.
- **Power zoom** is only driven on Sony.

## Windows and USB

On Windows, Studio opens a camera on the USB cable through libusb. The camera then needs the WinUSB driver (for example
with Zadig). **Studio never installs or changes a driver itself**: it says so on screen when a camera is on the cable
but cannot be opened. Wi-Fi needs no driver (Sony, Canon, Nikon, Fujifilm, Panasonic, OM System / Olympus,
Ricoh / Pentax).

## Where the codes come from

- The PTP standard (ISO 15740) for the plain PTP route.
- libgphoto2's `camlibs/ptp2` (`ptp.h`, `ptp-pack.c`, `config.c`, `library.c`), checked 14 November 2026, for the Canon
  and Nikon operation and property codes and the Canon event layout. Only the numbers are used; no code is copied.
- Wi-Fi, all checked 15 November 2026, numbers, names and packet layouts only, no code copied:
  libgphoto2 `camlibs/ptp2/fujiptpip.c` and `library.c` (Fujifilm Wi-Fi) and `camlibs/lumix/lumix.c` (Panasonic);
  the open LUMIX remote projects such as jayem1427/lumix-remote; the OPC protocol notes collected for the E-M10 and
  TG-5 (olympus-wifi, libqt-omd); CursedHardware/ricoh-wireless-protocol (an OpenAPI description of the Ricoh / Pentax
  camera API, from the Image Sync app).
- Sony's own PTP extension, as before (the protocol Sony's Camera Remote SDK also uses).

The per-maker data (vendor ids, which driver, setup steps, what is tested) is in
[`camctl/drivers/cameras.json`](../camctl/drivers/cameras.json). A model is listed there only after it was tested
with a real camera.

## Official SDKs

Sony's Camera Remote SDK, Canon's EDSDK and Nikon's SDK need a registration with the maker, and their licences do not
allow shipping them inside this MIT-licensed project. Studio therefore has only a plug-in point
(`camctl.drivers.registry.register_sdk(brand, factory)`): someone who registered with a maker can write a small adapter
on their own computer that returns an object with the driver methods listed in `camctl/drivers/base.py`. When one is
registered, it is used for that maker before Studio's own driver. None is included.

## Trying the drivers without a camera

    python tools/sim_studio.py --brand canon     # or nikon, or ptp

runs Studio on a simulated camera of that maker through the real driver code (tests/fake_ptp_brands.py). The simulated
cameras follow the documented codes; a real camera may differ, which is why every one of these routes is untested.

    python tools/sim_studio.py --wifi lumix      # or olympus, ricoh, fuji

does the same for the Wi-Fi routes (tests/fake_wifi_brands.py).
