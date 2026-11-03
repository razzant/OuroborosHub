/* Memory Atlas entry (ESM, run after the host's bridge bootstrap).
 *
 * - Theme: the host's resolved palette through OuroborosWidget.onTheme, applied as data-theme on this
 *   document (render.appearance: host). The root is `.ouro-ui`, so the installed kit's capsule recipes apply.
 * - Controls: the installed web/ui.css from this skill's `author-kit` route, then atlas.css, which lays out
 *   with the kit's tokens and adds no competing primitives. Without the kit the frame stays readable in
 *   native colours.
 * - Data: one client of the memory-atlas.read/2 routes over the bridged fetch.
 */
import { createClient, SKILL_PREFIX } from './client.js';
import { mountAtlas } from './atlas.js';

const doc = document.documentElement;
const root = document.getElementById('root');
const bridge = window.OuroborosWidget;
const bridgedFetch = (bridge && typeof bridge.fetch === 'function') ? bridge.fetch.bind(bridge) : window.fetch.bind(window);

doc.lang = 'en';
doc.classList.add('ouro-ui');
let themeOff = () => {};
const applyTheme = (theme) => { doc.dataset.theme = theme === 'light' ? 'light' : 'dark'; };
if (bridge && typeof bridge.onTheme === 'function') {
  themeOff = bridge.onTheme(applyTheme);
}
if (!doc.dataset.theme) { applyTheme(window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'); }

const viewport = document.createElement('meta');
viewport.name = 'viewport';
viewport.content = 'width=device-width, initial-scale=1';
document.head.append(viewport);

const kitStyle = document.createElement('style');
kitStyle.id = 'ouro-kit';
document.head.append(kitStyle);
const own = document.createElement('link');
own.rel = 'stylesheet';
own.href = new URL('./atlas.css', import.meta.url).href;
const ownLoaded = new Promise((resolve) => { own.onload = resolve; own.onerror = resolve; });
document.head.append(own);

let kit = false;
try {
  const response = await bridgedFetch(SKILL_PREFIX + 'author-kit', { timeoutMs: 15000 });
  if (response.ok) {
    const assets = await response.json();
    if (assets && typeof assets.css === 'string' && assets.css) { kitStyle.textContent = assets.css; kit = true; }
  }
} catch (error) {
  kit = false;   // the plain style below keeps every word readable
}
await ownLoaded;

const app = mountAtlas(root, { client: createClient({ fetch: bridgedFetch }), kit });
window.__memoryAtlas = app;
if (typeof window.__ouroWidgetOnDispose === 'function') {
  window.__ouroWidgetOnDispose(() => { themeOff(); app.dispose(); });
}
