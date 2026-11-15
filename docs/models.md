# Camera models that should work

<!-- Written by tools/models_doc.py from camctl/drivers/usb_models.json and cameras.json. Do not edit by hand: run python tools/models_doc.py -->

**Tested with a real camera: only the Sony ZV-E10** (firmware 2.03). Everything below is **untested**: written from public documentation (the PTP standard, libgphoto2's camera list, the makers' published Wi-Fi notes) and tried on simulated cameras only. "Should work" means Studio has a route for that camera, not that anyone has checked it.

Model names and USB ids come from libgphoto2's camera list (789 entries, 766 model names). Names are as libgphoto2 spells them, so one camera can appear twice under two spellings. Got one of these cameras? [Help add your camera](../README.md#help-add-your-camera): a camera check report from your camera moves it to tested.

## Levels

- **Full**: settings, photo, video and the live picture over one USB cable.
- **Settings and photo**: settings and shutter over USB; the live picture comes from the camera's own webcam mode (Studio picks it up by itself) or from an HDMI capture card.
- **Recognised only**: Studio names the camera and tells you what to change (for example the USB mode), but cannot control it in that mode or with that model.

Not supported: GoPro and Blackmagic (they are controlled through their own apps). On Windows, a camera on the USB cable needs the WinUSB driver (for example with Zadig); Studio never installs a driver itself. Wi-Fi needs no driver.

## Overview

| Maker | Full | Settings and photo | Recognised only | Wi-Fi |
|---|---|---|---|---|
| [Sony](#sony) | 43 | 8 | 86 | yes, over your Wi-Fi (port 15740) |
| [Canon](#canon) | 79 | 5 | 202 | yes, over your Wi-Fi (port 15740) |
| [Nikon](#nikon) | 52 | 95 | 40 | yes, over your Wi-Fi (port 15740) |
| [Fujifilm](#fujifilm) | 0 | 22 | 62 | yes, over your Wi-Fi (port 55740) |
| [Panasonic](#panasonic) | 0 | 2 | 15 | yes, the camera's own network (192.168.54.1) |
| [OM System / Olympus](#om-system--olympus) | 0 | 6 | 20 | yes, the camera's own network (192.168.0.10) |
| [Ricoh / Pentax](#ricoh--pentax) | 0 | 0 | 22 | yes, the camera's own network (192.168.0.1) |
| [Leica](#leica) | 0 | 6 | 1 | no |

Every Wi-Fi route except Sony's is untested. How each one works: [docs/cameras.md](cameras.md#routes).

## Sony

The ZV-E10 is the only camera tested for real. Every other Sony uses the same driver, untested. A Sony in MTP / mass-storage mode is recognised but not controlled until USB is set to PC Remote. Recognised only below means older Sonys without PC Remote.

**Wi-Fi** (tested on the ZV-E10): MENU > Network > PC Remote Function: On; Studio finds it on your Wi-Fi.

**Full** (43): A6700, Alpha-A5100, Alpha-A6100, Alpha-A6300, Alpha-A6500, Alpha-A6600, Alpha-A68, Alpha-A7 III, Alpha-A7 IV, Alpha-A77 M2, Alpha-A7III, Alpha-A7r II, Alpha-A7r III, Alpha-A7S, Alpha-A7S II, Alpha-A9, Alpha-A99 M2, Alpha-RX10M4, Alpha-RX1R II, DSC RX0 II, DSC-A7r IV, DSC-A7S III, DSC-RX0, DSC-RX100M4, DSC-RX100M5A, DSC-RX100M6, DSC-RX100M7, DSC-RX10M3, ILCE-1, ILCE-1M2, ILCE-6400, ILCE-7C, ILCE-7CM2, ILCE-7M2, ILCE-7M5, ILCE-7RM3A, ILCE-7RM4A, ILCE-7RM5, ILCE-9M3, ILME-FX3, ILX-LR1, ZV-1, ZV-E10

**Settings and photo** (8): Alpha-A5000, Alpha-A6000, Alpha-A7, Alpha-A7r, DSC-A900, DSC-RX100M5, DSC-RX100V, SLT-A58

**Recognised only** (86): Alpha-A3000, Alpha-A7R III, DSC-A100, DSC-F707V, DSC-F717, DSC-F828, DSC-H1, DSC-H2, DSC-H5, DSC-HX100V, DSC-HX200V, DSC-HX20V, DSC-HX300, DSC-HX400V, DSC-HX5V, DSC-HX60V, DSC-HX80, DSC-HX90V, DSC-N2, DSC-P10, DSC-P100, DSC-P120, DSC-P200, DSC-P30, DSC-P31, DSC-P32, DSC-P41, DSC-P43, DSC-P5, DSC-P50, DSC-P51, DSC-P52, DSC-P71, DSC-P72, DSC-P73, DSC-P92, DSC-P93, DSC-QX30U, DSC-R1, DSC-RX1, DSC-RX100, DSC-RX100M2, DSC-S40, DSC-S60, DSC-S730, DSC-S75, DSC-S780, DSC-S85, DSC-T1, DSC-T10, DSC-T3, DSC-U10, DSC-U20, DSC-V1, DSC-W1, DSC-W12, DSC-W130, DSC-W200, DSC-W35, DSC-W510, DSC-W55, DSC-WX220, DSC-WX350, HDR-PJ260VE, HDR-PJ710V, ILCE-7R M2, MVC-CD300, MVC-CD500, NEX-3N, NEX-5N, NEX-5R, NEX-6, NEX-7, NEX5, RX100M3, SLT-A35, SLT-A350, SLT-A37, SLT-A55, SLT-A57, SLT-A65V, SLT-A77V, SLT-A99V, SLT-A99v, UMC-R10C, ZV-E10M2

## Canon

Only EOS bodies (DSLR, EOS M and EOS R) have the remote mode Studio uses; PowerShot and IXUS stay recognised only. The mode dial and the lens AF / MF switch stay on the camera.

**Wi-Fi** (untested): MENU > Wireless settings > Wi-Fi function > Remote control (EOS Utility): put the camera on your Wi-Fi and pair it with this PC the first time.

**Full** (79): EOS 1000D, EOS 100D, EOS 1100D, EOS 1200D, EOS 1300D, EOS 1500D, EOS 1D C, EOS 1D Mark IV, EOS 1D X, EOS 1D X MarkII, EOS 1D X MarkIII, EOS 2000D, EOS 200D, EOS 250D, EOS 4000D, EOS 40D, EOS 450D, EOS 500D, EOS 50D, EOS 550D, EOS 5D Mark II, EOS 5D Mark III, EOS 5D Mark IV, EOS 5DS, EOS 5DS R, EOS 5Rm2, EOS 600D, EOS 60D, EOS 650D, EOS 6D, EOS 6d Mark II, EOS 700D, EOS 70D, EOS 750D, EOS 760D, EOS 77D, EOS 7D, EOS 7D MarkII, EOS 800D, EOS 80D, EOS 850D, EOS 90D, EOS Kiss X2, EOS Kiss X3, EOS M, EOS M10, EOS M100, EOS M2, EOS M200, EOS M3, EOS M5, EOS M50, EOS M50m2, EOS M6, EOS M6 Mark II, EOS R, EOS R1, EOS R10, EOS R100, EOS R3, EOS R5, EOS R5 C, EOS R50, EOS R50 V, EOS R6, EOS R6 Mark III, EOS R6m2, EOS R7, EOS R8, EOS Rebel T1i, EOS Rebel T6, EOS Rebel T7i, EOS Rebel T8i, EOS Rebel XSi, EOS RP, Rebel T2i, Rebel T3, Rebel T3i, Rebel T4i

**Settings and photo** (5): EOS 1D Mark III, EOS 30D, EOS 400D, EOS Digital Rebel XTi, EOS Kiss Digital X

**Recognised only** (202): Digital IXUS 100 IS, Digital IXUS 110 IS, Digital IXUS 120 IS, Digital IXUS 130, Digital IXUS 132, Digital IXUS 180, Digital IXUS 185, Digital IXUS 255HS, Digital IXUS 30, Digital IXUS 300 HS, Digital IXUS 40, Digital IXUS 400, Digital IXUS 430, Digital IXUS 50, Digital IXUS 500, Digital IXUS 55, Digital IXUS 60, Digital IXUS 65, Digital IXUS 70, Digital IXUS 700, Digital IXUS 75, Digital IXUS 750, Digital IXUS 80 IS, Digital IXUS 800, Digital IXUS 85 IS, Digital IXUS 850 IS, Digital IXUS 860 IS, Digital IXUS 90 IS, Digital IXUS 900Ti, Digital IXUS 95 IS, Digital IXUS 950 IS, Digital IXUS 960 IS, Digital IXUS 970 IS, Digital IXUS 980 IS, Digital IXUS 990 IS, Digital IXUS i, Digital IXUS II, Digital IXUS IIs, Digital IXUS iZ, Digital IXUS v3, Digital IXUS Wireless, Digital PowerShot SX730HS, Elura 50, Elura 65, EOS 10D, EOS 1D Mark II, EOS 20D, EOS 300D, EOS 350D, EOS 5D, EOS Digital Rebel, EOS Kiss Digital, IXUS 125HS, IXUS 160, IXUS 165, IXUS 310IS, IXY 220 IS, IXY 3000 IS, MV750i, MVX3i, Optura 600, PowerShot A1000 IS, PowerShot A1100 IS, PowerShot A1300IS, PowerShot A1400, PowerShot A2000 IS, PowerShot A2100 IS, PowerShot A2200, PowerShot A2300IS, PowerShot A2400IS, PowerShot A2500, PowerShot A2600, PowerShot A300, PowerShot A3000 IS, PowerShot A310, PowerShot A3100 IS, PowerShot A3500IS, PowerShot A400, PowerShot A4000IS, PowerShot A410, PowerShot A420, PowerShot A430, PowerShot A450, PowerShot A460, PowerShot A470, PowerShot A480, Powershot A495, PowerShot A510, PowerShot A520, PowerShot A530, PowerShot A540, PowerShot A550, PowerShot A560, PowerShot A570 IS, PowerShot A580, PowerShot A590 IS, PowerShot A60, PowerShot A610, PowerShot A620, PowerShot A630, PowerShot A640, PowerShot A650IS, PowerShot A70, PowerShot A700, PowerShot A710 IS, PowerShot A720 IS, PowerShot A75, PowerShot A80, PowerShot A800, PowerShot A810, PowerShot A85, PowerShot A95, PowerShot D10, PowerShot ELPH 350 HS, PowerShot Elph135, PowerShot Elph340HS, PowerShot G1 X, PowerShot G10, PowerShot G11, PowerShot G12, PowerShot G15, PowerShot G16, PowerShot G3, PowerShot G3 X, PowerShot G5, PowerShot G5X, Powershot G6, PowerShot G7, PowerShot G7 X, PowerShot G7 X Mark II, PowerShot G7 X Mark III, PowerShot G9, PowerShot G9 X Mark II, PowerShot S1 IS, PowerShot S100, PowerShot S110, PowerShot S120, PowerShot S2 IS, PowerShot S230, PowerShot S3 IS, PowerShot S400, PowerShot S410, PowerShot S45, PowerShot S5 IS, PowerShot S50, PowerShot S500, Powershot S60, Powershot S70, PowerShot S80, PowerShot S95, PowerShot SD100, PowerShot SD1000, PowerShot SD110, Powershot SD1100 IS, Powershot SD1200 IS, PowerShot SD1300 IS, PowerShot SD200, PowerShot SD40, PowerShot SD430, PowerShot SD450, PowerShot SD500, PowerShot SD600, PowerShot SD630, PowerShot SD700, PowerShot SD750, PowerShot SD770 IS, PowerShot SD780 IS, Powershot SD790 IS, PowerShot SD850, PowerShot SD880 IS, PowerShot SD900, PowerShot SD940 IS, PowerShot SD970 IS, PowerShot SD990, PowerShot SX10 IS, Powershot SX100 IS, PowerShot SX110 IS, PowerShot SX120 IS, PowerShot SX130 IS, PowerShot SX150 IS, PowerShot SX160IS, PowerShot SX170 IS, PowerShot SX20 IS, PowerShot SX200 IS, PowerShot SX210 IS, PowerShot SX220HS, PowerShot SX230HS, PowerShot SX240HS, PowerShot SX260HS, PowerShot SX280HS, PowerShot SX40HS, PowerShot SX500IS, PowerShot SX510 HS, PowerShot SX520 HS, PowerShot SX530 HS, PowerShot SX540 HS, PowerShot SX600 HS, PowerShot SX60HS, PowerShot SX70 HS, PowerShot SX740 HS, SX 620HS, SX 720HS

## Nikon

Bodies listed as settings and photo take pictures over USB but give no live picture over the cable.

**Wi-Fi** (untested): Network menu > Connect to computer (Z bodies; older bodies with built-in Wi-Fi or a WU-1a / WU-1b adapter). Pair it with this PC on the camera the first time.

**Full** (52): Coolpix B700, Coolpix P1000, Coolpix P1100, Coolpix S9700, DSC D300s, DSC D3200, DSC D3300, DSC D3400, DSC D3500, DSC D3s, DSC D3x, DSC D4, DSC D4s, DSC D5, DSC D500, DSC D5000, DSC D5100, DSC D5200, DSC D5300, DSC D5500, DSC D5600, DSC D6, DSC D600, DSC D610, DSC D700, DSC D7000, DSC D7100, DSC D7200, DSC D750, DSC D7500, DSC D780, DSC D800, DSC D800E, DSC D810, DSC D810A, DSC D850, DSC D90, DSC Df, P950, Z30, Z5, Z50, Z50_2, Z6, Z6 III, Z6_2, Z7, Z7_2, Z8, Z9, Zf, Zfc

**Settings and photo** (95): Coolpix 2200, Coolpix 2200v1.1, Coolpix 3700, Coolpix 4100, Coolpix 4600a, Coolpix 5000, Coolpix 5400, Coolpix 5600, Coolpix 5700, Coolpix 5900, Coolpix 7600, Coolpix 7900, Coolpix 8800, Coolpix 9400, Coolpix A, Coolpix A900, Coolpix AW100, Coolpix AW110, Coolpix AW130, Coolpix B500, Coolpix L100, Coolpix L110, Coolpix L12, Coolpix L120, Coolpix L16, Coolpix L19, Coolpix L20, Coolpix L23, Coolpix L27, Coolpix L340, Coolpix L810, Coolpix L820, Coolpix L840, Coolpix P1, Coolpix P100, Coolpix P2, Coolpix P330, Coolpix P4, Coolpix P500, Coolpix P5000, Coolpix P60, Coolpix P6000, Coolpix P7000, Coolpix P7100, Coolpix P80, Coolpix P80 v1.1, Coolpix P900, Coolpix S01, Coolpix S02, Coolpix S200, Coolpix S220, Coolpix S225, Coolpix S2500, Coolpix S2600, Coolpix S2700, Coolpix S2900, Coolpix S3000, Coolpix S3100, Coolpix S3200, Coolpix S3300, Coolpix S3500, Coolpix S3600, Coolpix S3700, Coolpix S4300, Coolpix S6300, Coolpix S6800, Coolpix S9100, Coolpix S9500, D2Hs, D2X SLR, D3, D50, DSC D200, DSC D2Xs, DSC D300, DSC D3000, DSC D3100, DSC D40, DSC D40x, DSC D60, DSC D70, DSC D70s, DSC D80, J1, J2, J3, J4, J5, KeyMission 170, KeyMission 360, S1, S2, V1, V2, V3

**Recognised only** (40): Coolpix 2000, Coolpix 2100, Coolpix 2500, Coolpix 3100, Coolpix 3200, Coolpix 3500, Coolpix 4200, Coolpix 4300, Coolpix 4500, Coolpix 4600, Coolpix 4800, Coolpix 5200, Coolpix 8700, Coolpix 885, Coolpix L1, Coolpix L10, Coolpix L11, Coolpix L4, Coolpix P300, Coolpix P50, Coolpix P510, Coolpix P5100, Coolpix P520, Coolpix P7700, Coolpix P7800, Coolpix P90, Coolpix S2, Coolpix S4, Coolpix S500, Coolpix S5100, Coolpix S6, Coolpix S60, Coolpix S6000, Coolpix S620, Coolpix S7c, Coolpix S8000, Coolpix S8200, Coolpix SQ, D2H SLR, DSC D100

## Fujifilm

Standard PTP over USB (settings it lists, shutter). For the live picture use the camera's webcam mode (X Webcam) or an HDMI capture card.

**Wi-Fi** (untested): Connection setting > PC Shoot / PC connection over Wi-Fi (bodies FUJIFILM X Acquire controls over Wi-Fi, such as X-T3, X-T4, X-H2). Pair it with this PC on the camera.

**Settings and photo** (22): Fujifilm GFX 100S, Fujifilm GFX100, Fujifilm GFX100 II, Fujifilm GFX100S II, Fujifilm X-E3, Fujifilm X-H1, Fujifilm X-H2, Fujifilm X-H2S, Fujifilm X-M5, Fujifilm X-Pro2, Fujifilm X-Pro3, Fujifilm X-S20, Fujifilm X-T1, Fujifilm X-T2, Fujifilm X-T3, Fujifilm X-T4, Fujifilm X-T5, Fujifilm X-T50, Fujifilm X100VI, GFX 50 R, GFX 50 S, S5 Pro

**Recognised only** (62): FinePix A330, FinePix A610, FinePix A800, FinePix A820, FinePix A920, FinePix AV-150, FinePix E900, FinePix F100fd, FinePix F20, FinePix F200 EXR, FinePix F30, FinePix F31fd, FinePix F40fd, FinePix F50fd, FinePix F60fd, FinePix F70 EXR, FinePix F80EXR, FinePix H20EXR, FinePix HS30EXR, FinePix JX370, FinePix S1000fd, FinePix S100fs, FinePix S1500, FinePix S1800, FinePix S2000HD, FinePix S2500HD, FinePix S2950, FinePix S2980, FinePix S4200, FinePix S4300, FinePix S4850, FinePix S5700, FinePix S5800, FinePix S6500fd, FinePix S7000, FinePix S8600, FinePix S9500, FinePix SL1000, FinePix T200, FinePix X-E1, FinePix X-S1, FinePix X10, FinePix X20, FinePix XF1, FinePix Z100fd, FinePix Z35, FinePix Z700EXR, Fujifilm A220, Fujifilm X-A2, Fujifilm X-A5, Fujifilm X-E2, Fujifilm X-E4, Fujifilm X-E5, Fujifilm X-M1, Fujifilm X-S10, Fujifilm X-T10, Fujifilm X-T20, Fujifilm X-T30, Fujifilm X100F, Fujifilm X100V, Fujifilm X30, Fujifilm X70

## Panasonic

Standard PTP over USB. Wi-Fi also brings the live picture; a photo taken over Wi-Fi stays on the card.

**Wi-Fi** (untested): Wi-Fi > Wi-Fi Function > New Connection > Remote Shooting & View; join the camera's Wi-Fi network from this PC and confirm the connection on the camera.

**Settings and photo** (2): DC-GH5, DMC-GX8

**Recognised only** (15): DMC-FS62, DMC-FZ20, DMC-FZ38, DMC-FZ45, DMC-FZ50, DMC-GF1, DMC-LC1, DMC-LS2, DMC-LS3, DMC-LX7, DMC-LZ2, DMC-TZ15, DMC-TZ18, DMC-TZ8, Lumix FZ5

## OM System / Olympus

Standard PTP over USB. Wi-Fi also brings the live picture.

**Wi-Fi** (untested): Connection to Smartphone (or the Wi-Fi button): join the camera's own Wi-Fi network from this PC.

**Settings and photo** (6): E-M1, E-M1 Mark II, E-M1 MII, E-M5 Mark II, OM-1, OM-1MarkII

**Recognised only** (20): C-310Z, C-350Z, C-5500Z, C-55Z, D-540Z, D-560Z, E series, E-410, E-M5, E-PL5, FE4000, IR-300, mju 500, SP-720UZ, TG-620, VR360, X-100, X-250, X920, X925

## Ricoh / Pentax

No model is controllable over USB in libgphoto2's list. Wi-Fi is the route for bodies with the Wi-Fi API (Ricoh GR III / GR IIIx, Pentax K-1 II, KP, K-3 III and similar), untested.

**Wi-Fi** (untested): Switch the camera's Wi-Fi on (GR: Setup > Wi-Fi; Pentax K-1 / KP / K-3 III: Wireless LAN) and join its network from this PC.

**Recognised only** (22): Caplio GX, Caplio GX 8, Caplio R1v, Caplio R3, Caplio R5, Caplio RR750, GR III, GR IIIx, K-3 Mark III Monochrome, K1, K3, K3 II, K70, KP, Optio 43WR, Optio W90, Theta m15, Theta S, Theta SC, Theta V, Theta Z1, WG-M2

## Leica

Standard PTP over USB only; no Wi-Fi route and no live picture.

**Settings and photo** (6): M11 Monochrom, M9, Q3, Q3 43, SL, SL3

**Recognised only** (1): D-LUX 2
