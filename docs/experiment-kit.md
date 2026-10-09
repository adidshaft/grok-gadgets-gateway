# First ESP32-S3 and OLED experiment

The owner ordered this kit on 9 October 2026:

| Item | Ordered description | Verification needed |
| --- | --- | --- |
| Board | AYWHP ESP32-S3 DevKitC-style, ESP32-S3-WROOM-1-N16R8, USB-C | Read module and board markings; identify USB ports, header layout, and memory configuration. |
| Wiring | ApTechDeals 840-point breadboard and jumper-wire set | Check board fit, header soldering, wire ends, and rail continuity. |
| Display | Robocraze 0.96-inch 128×64 OLED, advertised as SPI/I²C, 4-pin | Confirm controller, interface, pin order, voltage, and address. |

These are order details, not inspected hardware facts.
This kit replaces the AtomS3 Lite first-experiment assumption.
The existing C124 firmware remains a separate example. Do not flash it onto this board.

## Software first

Run the existing gateway simulator and `grok-gadgets-gateway rehearse` before delivery.
This checks the gateway's software route. It does not drive an OLED.
The legacy `sim-c124` identifier remains for compatibility.
Use the ESP32 SDK's OLED software rehearsal for bounded text and display state checks.
From the ESP32 SDK checkout, run `python3 examples/oled-kit/rehearse.py`.
It needs no display controller or pin mapping.

## Physical route after delivery

The proposed path is: host gateway → host USB bridge → ESP32-S3 → breadboard wiring → OLED.
Grok Bot uses a separately verified connection to the gateway.
A local rehearsal does not verify that connection.

1. Inspect the board, module, display markings, headers, and supplied wiring.
2. Confirm the display controller, interface, voltage, pin order, and address from matching documentation.
3. Record the board's actual USB data port and firmware settings. Confirm a USB-C data cable is available.
4. Select usable GPIOs only after checking the exact board and module restrictions.
5. Prepare and compile matching firmware. Record the SDK commit, configuration, and firmware hash.
6. Wire the display with power disconnected. Check every connection before applying power.
7. Verify text, clear, state, and USB disconnect/reconnect on the real display.
8. Add a suitable external LED with a current-limiting resistor and a button if onboard parts are unsuitable.
9. Record each physical observation separately from command acknowledgements and Grok Bot results.

The LED, resistor, button, and data cable are not confirmed parts of the order.
Do not use a reset button as a normal input without checking its function.
No GPIO numbers, OLED driver, bus address, or wiring voltage are confirmed yet.
The SPI/I²C advertisement does not establish the delivered interface.

## Acceptance

- Software: discover the simulated gadget; reject invalid commands; report state and offline status accurately.
- OLED rehearsal: check bounded text, clear, button transitions, and recovery without a driver.
- Hardware: observe legible text and a cleared display, then compare reported state with the physical result.
- Input/output: observe button press/release and LED on/off only for confirmed, connected components.
- Recovery: test unplug, reconnect, and reboot without replaying uncertain actions.
- Evidence: record the inspected parts, wiring table, host, firmware hash, versions, and Pass/Fail/Not tested results.
- Grok Bot: retain a separate connection and invocation evidence gate.

Follow the [hub experiment guide](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/getting-started/experiment-kit.md)
and [ESP32 SDK](https://github.com/adidshaft/grok-gadgets-esp32-sdk).
The physical gate remains open until delivery, inspection, matching firmware, and observed checks.

## Local checks on 9 October 2026

All 148 gateway tests and Ruff checks passed after this documentation update.
No gateway runtime, protocol, or packaged simulator input changed.
The existing simulator remains an RGB/button fixture, not an OLED implementation.
