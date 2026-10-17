# Third-party notices and protocol references

This project is an independent implementation of an unofficial, reverse-engineered ASIAIR network protocol.

Protocol behavior and implementation ideas were cross-checked against:

- `ashleywbrown/asiair-mqtt` (MIT), copyright (c) 2025 Ashley Brown; AstroLive portions copyright (c) 2022 Markus Winkler.
- `StefanDorresteijn/asiair-dashboard` (MIT), particularly its ASIAIR protocol documentation.
- `irjudson/seestar-api`, particularly the documented current ZWO 4700 verification handshake and RSA public key.
- `Jiang-sustech/REA-ZWO`, RPC string inventory from ASIAIR binaries; `frankhirsch/ASIAIRstatus`, public mount traces. The current `scope_park`, `scope_set_track_state`, `start_exposure`, and `stop_exposure` requests were additionally verified against local captures of the official ASIAIR app (captures are not redistributed).

ZWO and ASIAIR are trademarks of their respective owner. This project is not an official ZWO product.
