# Example apps.yaml
```
twitch_firetvappstate_handshake:
  module: twitch_firetvappstate
  class: Handshake
  host: localhost
  port: 5555
  out_dir: /config/app/firetvappstate

twitch_firetvappstate:
  module: twitch_firetvappstate
  class: TwitchPlayback
  host: localhost
  port: 5555
  adbkey: /config/app/firetvappstate/file.key
  entity_prefix: firetv_twitch
  poll_secs: 5

```