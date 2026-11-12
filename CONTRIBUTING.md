# Contributing

The most useful help right now is a **camera report** from a camera that is not yet in the tested list.

## Share a camera report from Studio

1. Connect your camera to Studio (USB or Wi-Fi).
2. All settings › Studio › **Check this camera** (about two minutes, no photo is taken; settings are put back).
3. Press **Share**: a prefilled issue opens in your browser. Drag the report file onto the page and press Submit.

Studio removes the serial number, names you gave the camera, network details, paths and pictures. Nothing is sent
without your press. Every report is read; a camera is added to the tested list in the README when it works.

## File a camera report by hand

Open a new issue with the **Camera report** template and fill in brand, model, firmware, connection and what worked.
If you have the report file from "Save report" (All settings › Studio › Check this camera), attach it.

## Code

`python -m pytest tests` runs everything on a simulated camera. Please keep personal data out of the code and tests
(`python tools/publish.py --check` must pass). A second camera brand would plug in at `camctl/drivers`
(`drivers/base.py` lists the functions Studio needs).
