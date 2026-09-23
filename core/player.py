"""The backend-hosted video player page served at GET /player.

The frontend plays iframe sources well but only links raw hls/mp4, so a resolver
can wrap its stream in ``/player?type=hls&src=/jellyfin_proxy/...`` and hand the
frontend an iframe instead. Player and stream share the backend origin, so
hls.js needs no CORS, and ``src`` is restricted to same-origin paths so the page
cannot embed external content.
"""

import json
from html import escape
from string import Template

PLAYER_COLOR = "C20000"


def is_safe_src(src: str) -> bool:
    """Only same-origin relative paths, e.g. "/jellyfin_proxy/...". Browsers read
    a backslash as a slash, so "/\\host" is as protocol-relative as "//host"."""
    return bool(src) and src.startswith("/") and src[1:2] not in ("/", "\\")


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


def render_player(src: str, stream_type: str = "", title: str = "") -> str:
    if not stream_type:
        stream_type = "hls" if ".m3u8" in src.lower() else "mp4"
    # json.dumps alone does not stop "</script>" in src from closing the script
    # element, so the HTML-significant characters are escaped as JS unicode escapes.
    cfg = (
        json.dumps({"src": src, "type": stream_type})
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return _TEMPLATE.safe_substitute(
        title=escape(title or "Crimson Player"),
        color=PLAYER_COLOR,
        cfg=cfg,
    )
