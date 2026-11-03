"""Send the plugin's one hard-coded provider authority to the controlled loopback Bot API.

Loaded by the host process through PYTHONPATH (this directory only, never a product file). The plugin's own
``TelegramClient`` and its ``urllib`` calls run unchanged: only the scheme and authority of a URL that begins
``https://api.telegram.org`` are replaced before ``urllib`` opens it. Anything else is left alone, so a host
call to loopback is not touched and no other Telegram or internet address is reachable (the fixture also sets an
unroutable HTTPS proxy as a second fence). The proof that this ran is a file this module writes per process.
"""
import json
import os
import urllib.request

_REAL = "https://api.telegram.org"
_FAKE = os.environ.get("TG_FAKE_BOT_API", "").rstrip("/")
_PROOF = os.environ.get("TG_SHIM_PROOF", "")

if _FAKE:
    _open = urllib.request.urlopen

    def urlopen(url, *args, **kwargs):
        full = url.full_url if isinstance(url, urllib.request.Request) else url
        if isinstance(full, str) and full.startswith(_REAL):
            rewritten = _FAKE + full[len(_REAL):]
            if isinstance(url, urllib.request.Request):
                url.full_url = rewritten
            else:
                url = rewritten
        return _open(url, *args, **kwargs)

    urllib.request.urlopen = urlopen
    if _PROOF:
        with open(_PROOF + "." + str(os.getpid()), "w", encoding="utf-8") as proof:
            json.dump({"pid": os.getpid(), "real": _REAL, "fake": _FAKE}, proof)
