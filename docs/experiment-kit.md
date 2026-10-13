# ESP32-S3 and OLED experiment

The owner tested the delivered kit on 12 October 2026. USB and Wi-Fi acceptance passed.
The delivered display differs from the order. Use the inspected parts below.

| Part | Inspected item |
| --- | --- |
| Board | YD-ESP32-23 v1.3, ESP32-S3 N16R8 |
| Display | 1.3-inch SH1106, 128×64, I²C address `0x3c` |
| Wiring | ApTechDeals breadboard and jumpers; VCC to 3V3, GND to GND, SCK to GPIO9, SDA to GPIO8 |
| Input/output | RGB pixel on GPIO48; BOOT button on GPIO0 |
| USB | Native **USB** port, not **COM**; USB-C data cable |

Inspect your own parts before wiring. Disconnect power first.
Do not use the C124 firmware on this board.

## Start with software

Run the gateway simulator and `grok-gadgets-gateway rehearse` first.
This checks software. It does not operate a physical OLED.
The ESP32 SDK also has an OLED software model: `python3 examples/oled-kit/rehearse.py`.

## Repeat the physical test

Follow the [ESP32 kit guide](https://github.com/adidshaft/grok-gadgets-esp32-sdk/blob/dev/docs/first-experiment.md).
It contains the parts, wiring, build steps and a results table.
Start with USB. Then follow [Wi-Fi setup](wifi-devices.md).

Keep the gateway running on the Mac. The Wi-Fi board can use a USB charger; it still needs the gateway computer.
The owner verified the [always-on service](always-on.md) on this kit. An independent second installation is not verified.

## Evidence and limits

- [USB results](https://github.com/adidshaft/grok-gadgets-esp32-sdk/blob/dev/docs/verification/oled-kit-2026-10-12.md)
- [Wi-Fi results](https://github.com/adidshaft/grok-gadgets-esp32-sdk/blob/dev/docs/verification/oled-kit-wifi-2026-10-12.md)
- [Grok Bot local-command test](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/grok-local-execution-2026-10-12.md)

Grok Bot ran a local `rehearse` command. On 13 October 2026 it also called the tools natively through an owner-run tunnel with sign-in ([record](https://github.com/adidshaft/grok-gadgets/blob/main/docs/verification/grok-remote-https-2026-10-13.md)).
Record physical observations separately from gateway acknowledgements. A client name alone does not prove which product sent a request.
