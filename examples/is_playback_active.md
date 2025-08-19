1|kara:/ $ dumpsys media_session | grep "  TwitchMediaSession" -A14
    TwitchMediaSession tv.twitch.android.viewer/TwitchMediaSession (userId=0)
      ownerPid=27933, ownerUid=10205, userId=0
      package=tv.twitch.android.viewer
      launchIntent=null
      mediaButtonReceiver=null
      active=true
      flags=3
      rating type=0
      controllers: 3
      state=PlaybackState {state=3, position=2188000, buffered position=0, speed=1.0, updated=833820732, actions=847, custom actions=[], active item id=-1, error=null}
      audioAttrs=AudioAttributes: usage=USAGE_MEDIA content=CONTENT_TYPE_UNKNOWN flags=0x0 tags= bundle=null
      volumeType=1, controlType=2, max=0, current=0
      metadata:size=0, description=null
      queueTitle=null, size=0

      state=1; not playing back, at channel select
      state=3; active playback
      state=6; unknown but observed inbetween
