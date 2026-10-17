# Signed firmware updates

The relay can send a new firmware image to a gadget over the gadget's own TLS connection. Before it
downloads anything, the gadget checks a signature made with the builder's key. It checks the
image before it makes it bootable. When the new image does not come Online, the gadget goes
back to the previous image by itself. Design: [hub ADR 012](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/012-signed-updates.md).

Only gadgets whose firmware holds your public key ask for updates. Released firmware up to
ESP32 SDK 0.2.0-alpha.16 does not; flash an update-capable image over USB once.

## Roles

- **Builder** (your computer): owns the signing key, builds and signs images.
- **Relay operator** (the relay host): stores signed updates and assigns them to gadgets.

One person can be both. The private key never goes to the relay.

## 1. Make the signing key (once, on your computer)

```sh
grok-gadgets-gateway firmware keygen --out ~/grok-keys/builder.key
```

This writes `builder.key` (mode 0600) and `builder.pub.pem`, and prints the key ID. Back up
the private key offline. If you lose it, a cable is the only way to update gadgets that
trust only that key. Never commit it, copy it to the relay or paste it into a chat.

Compile `builder.pub.pem` into your firmware (see the ESP32 SDK guide). Give the relay a copy
of the public key.

## 2. Sign an image

```sh
grok-gadgets-gateway firmware sign .pio/build/esp32s3-oled-kit-wifi/firmware.bin \
  --key ~/grok-keys/builder.key --board esp32s3-oled-kit-wifi \
  --version 0.2.0-alpha.17 --out update-0.2.0-alpha.17
```

`--version` must be what the new image reports as `firmware_version` in its hello. `--board`
must match the board compiled into the gadget. The output directory holds `image.bin`,
`manifest.json` and `manifest.sig`; copy it to the relay host.

## 3. Store and assign (relay host)

```sh
grok-gadgets-gateway firmware add update-0.2.0-alpha.17 --public-key builder.pub.pem
grok-gadgets-gateway firmware assign oledkit-0123456789ab 0.2.0-alpha.17
grok-gadgets-gateway firmware status
```

`add` refuses an update whose signature does not match the public key, and an image whose
SHA-256 differs from the manifest. A running `serve` picks up the assignment at the gadget's next poll. You do not restart it. `firmware assign DEVICE --none` withdraws an offer.
`firmware list` and `firmware remove BOARD VERSION` manage stored updates.

`firmware add --unchecked` skips the signature check. Use it only to prove that a gadget
refuses an image signed with another key.

## What happens

1. The gadget gets the offer in its hello or poll reply and checks the signature, the board,
   the version and the size. On a failure the gadget reports `rejected` and downloads nothing.
2. It downloads 1,024-byte chunks into its other app slot, still answering commands.
3. It checks the byte count and SHA-256, makes the slot bootable and restarts.
4. The new image marks itself valid only after it is Online through the relay, then reports
   `applied`. If it is not Online within 5 minutes, or it crashes first, the previous image
   starts and reports `reverted`.

The relay does not offer a rejected, failed or reverted version to that gadget again until you
assign it again.

## Request log lines

`update_offered`, `update_started`, `update_progress` (25, 50 and 75 %), `update_verified`,
`update_applied`, `update_rejected`, `update_failed`, `update_reverted`, `update_busy` and
`update_withdrawn`. Each line has `device`, `version` and, when there is one, `reason`. No
line holds image bytes or key material.

## Limits

- At most 8 gadgets download at once; others get `busy` and retry.
- An image is at most 8 MiB. Chunks are read from disk for each request.
- A download that stops (power cut, lost connection) starts again from the beginning.
- Anti-rollback is off. The gadget accepts any signed version other than the running one, so you can push an older signed image again. Secure boot and flash encryption are off on the
  kit, so anyone with the board and a cable can flash it. See ADR 012 §8.

## Try it without a gadget

```sh
grok-gadgets-gateway rehearse --update
```

It signs updates with throwaway keys in a temporary directory. It delivers them to a
simulated gadget on a private relay. The gadget applies a good update after a restart. The gadget
refuses an image signed with another key and an image with one changed byte. An image that
never goes Online reverts. It proves the relay side only, not that a real gadget writes, verifies
or boots an image.
