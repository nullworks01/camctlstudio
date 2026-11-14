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
| gphoto2 | the cameras libgphoto2 supports | settings, live picture, photo | **untested**, Linux / macOS only | no: only if the `gphoto2` Python package is already installed, and `CAMCTL_ROUTE=gphoto2` is set |
| Maker SDK | Sony Camera Remote SDK, Canon EDSDK, Nikon SDK | whatever the adapter provides | plug-in point only | no |

Not supported: GoPro and Blackmagic cameras (they are controlled through their own apps / APIs, not PTP), Canon
PowerShots without EOS remote mode, Wi-Fi for Canon and Nikon.

## What stays on the camera

- **Canon**: the mode dial and the AF / MF switch on the lens. Set the dial to M.
- **Nikon**: the exposure mode on bodies with a mode dial.
- **Power zoom** is only driven on Sony.

## Windows and USB

On Windows, Studio opens a camera on the USB cable through libusb. The camera then needs the WinUSB driver (for example
with Zadig). **Studio never installs or changes a driver itself**: it says so on screen when a camera is on the cable
but cannot be opened. Sony cameras can use Wi-Fi instead, which needs no driver.

## Where the codes come from

- The PTP standard (ISO 15740) for the plain PTP route.
- libgphoto2's `camlibs/ptp2` (`ptp.h`, `ptp-pack.c`, `config.c`, `library.c`), checked 14 November 2026, for the Canon
  and Nikon operation and property codes and the Canon event layout. Only the numbers are used; no code is copied.
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
