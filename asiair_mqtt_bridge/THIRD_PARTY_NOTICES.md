# Third-party notices and protocol references

This project is an independent implementation of an unofficial, reverse-engineered ASIAIR network protocol.

Protocol behavior and implementation ideas were cross-checked against:

- `ashleywbrown/asiair-mqtt` (MIT), copyright (c) 2025 Ashley Brown; AstroLive portions copyright (c) 2022 Markus Winkler.
- `StefanDorresteijn/asiair-dashboard` (MIT), particularly its ASIAIR protocol documentation.
- `irjudson/seestar-api`, particularly the documented current ZWO 4700 verification handshake and RSA public key.

Runtime dependency:

- `paho-mqtt` 2.1.0 — dual-licensed under EPL-2.0 or Eclipse Distribution License 1.0 (EDL-1.0); used as the MQTT client library.

ZWO and ASIAIR are trademarks of their respective owner. This project is not an official ZWO product.
