"""Which web APIs / identity properties a page sees in servoshell (Chrome UA + site prefs), on the local login page."""
import json

from wd import SPIKE, Servo

PROBE = """const t = x => { try { return x(); } catch (e) { return 'throws: ' + e.message; } };
return {
  webdriver: String(navigator.webdriver), vendor: navigator.vendor, platform: navigator.platform, languages: navigator.languages,
  plugins: navigator.plugins ? navigator.plugins.length : 'missing', userAgentData: typeof navigator.userAgentData,
  window_chrome: typeof window.chrome, PublicKeyCredential: typeof window.PublicKeyCredential, credentials: typeof navigator.credentials,
  serviceWorker: typeof navigator.serviceWorker, Notification: typeof window.Notification, permissions: typeof navigator.permissions,
  webgl: t(() => !!document.createElement('canvas').getContext('webgl')), webgl2: t(() => !!document.createElement('canvas').getContext('webgl2')),
  WebAssembly: typeof WebAssembly, indexedDB: typeof indexedDB, BroadcastChannel: typeof BroadcastChannel, SharedWorker: typeof SharedWorker,
  crypto_subtle: typeof (crypto && crypto.subtle), IntersectionObserver: typeof IntersectionObserver, ResizeObserver: typeof ResizeObserver,
  adoptedStyleSheets: typeof document.adoptedStyleSheets, FontFace: typeof FontFace, animate: typeof document.body.animate,
  cookieStore: typeof window.cookieStore, PaymentRequest: typeof window.PaymentRequest, RTCPeerConnection: typeof window.RTCPeerConnection,
  hardwareConcurrency: navigator.hardwareConcurrency, deviceMemory: String(navigator.deviceMemory), screen: [screen.width, screen.height, devicePixelRatio],
  outer: [outerWidth, outerHeight], inner: [innerWidth, innerHeight], timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
}"""

if __name__ == '__main__':
    out = {}
    for prefs in (False, True):
        s = Servo(ua='chrome', prefs=prefs)
        d = s.session()
        d.get('http://127.0.0.1:8767/login.html')
        out['site-prefs' if prefs else 'default-prefs'] = d.js(PROBE)
        d.quit()
        s.close()
    (SPIKE / 'out/apis.json').write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
