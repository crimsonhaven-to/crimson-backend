"""
Backend-hosted video player page (served at GET /player).

A minimal, ad-free, Crimson-red themed player for direct streams that aren't a
self-contained embed (e.g. the Jellyfin HLS/MP4 streams from our /jellyfin_proxy).
The frontend plays "iframe" sources well but only shows a link for raw hls/mp4,
so resolvers can wrap their stream in ``/player?type=hls&src=/jellyfin_proxy/...``
and hand the frontend a normal iframe instead.

The player and the stream are served from the *same* (backend) origin, so hls.js
fetches the playlist/segments same-origin — no CORS needed. ``src`` is restricted
to same-origin relative paths (see ``is_safe_src``) so this can't be abused to
embed arbitrary external content.
"""

import json
from html import escape
from string import Template

PLAYER_COLOR_DEFAULT = "C20000"  # Crimson red, matching the other sources.


def is_safe_src(src: str) -> bool:
    """Only allow same-origin relative stream paths ("/jellyfin_proxy/..")."""
    return bool(src) and src.startswith("/") and not src.startswith("//")


_TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<meta name="referrer" content="no-referrer"/>
<title>$title</title>
<style>
  html,body{margin:0;height:100%;background:#000;overflow:hidden}
  #v{width:100%;height:100%;background:#000;display:block;accent-color:#$color}
  #spin{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
  #spin.hide{display:none}
  .ring{width:54px;height:54px;border:4px solid rgba(255,255,255,.18);border-top-color:#$color;border-radius:50%;animation:r .9s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  #err{position:fixed;inset:0;display:none;align-items:center;justify-content:center;color:#ddd;font-family:system-ui,-apple-system,sans-serif;text-align:center;padding:24px}
  #err h3{color:#$color;margin:0 0 8px}
</style>
</head>
<body>
<video id="v" controls autoplay playsinline></video>
<div id="spin"><div class="ring"></div></div>
<div id="err"><div><h3>Playback error</h3><p id="errmsg"></p></div></div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<script>
(function(){
  var CFG=$cfg;
  var v=document.getElementById('v'), spin=document.getElementById('spin');
  function hideSpin(){spin.classList.add('hide');}
  function showErr(m){spin.classList.add('hide');document.getElementById('errmsg').textContent=m||'';document.getElementById('err').style.display='flex';}
  v.addEventListener('playing',hideSpin); v.addEventListener('canplay',hideSpin);
  v.addEventListener('waiting',function(){spin.classList.remove('hide');});
  var isHls = CFG.type==='hls' || CFG.src.toLowerCase().indexOf('.m3u8')!==-1;
  if(isHls){
    if(window.Hls && window.Hls.isSupported()){
      var hls=new window.Hls({maxBufferLength:30});
      hls.on(window.Hls.Events.ERROR,function(_e,d){ if(d&&d.fatal){ showErr('Stream error: '+((d&&d.details)||(d&&d.type)||'')); } });
      hls.loadSource(CFG.src); hls.attachMedia(v);
      hls.on(window.Hls.Events.MANIFEST_PARSED,function(){ v.play().catch(function(){}); });
    } else if (v.canPlayType('application/vnd.apple.mpegurl')){
      v.src=CFG.src; v.addEventListener('loadedmetadata',function(){v.play().catch(function(){});});
    } else { showErr('HLS playback is not supported by this browser.'); }
  } else {
    v.src=CFG.src;
    v.addEventListener('loadeddata',function(){v.play().catch(function(){});});
    v.addEventListener('error',function(){showErr('Could not load video.');});
  }
})();
</script>
</body>
</html>"""
)


def render_player(src: str, stream_type: str = "", title: str = "", poster: str = "",
                  color: str = PLAYER_COLOR_DEFAULT) -> str:
    """Render the player HTML for a (same-origin) stream URL."""
    if not stream_type:
        stream_type = "hls" if ".m3u8" in src.lower() else "mp4"
    safe_color = "".join(c for c in (color or "") if c in "0123456789abcdefABCDEF") or PLAYER_COLOR_DEFAULT
    # json.dumps safely escapes src/type for embedding in the JS context.
    cfg = json.dumps({"src": src, "type": stream_type})
    return _TEMPLATE.safe_substitute(
        title=escape(title or "Crimson Player"),
        color=safe_color,
        cfg=cfg,
    )
