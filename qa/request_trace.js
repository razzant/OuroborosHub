// Temporary observer, installed before Card navigation in every document.
// Never drains/clones a body. It observes the product's reads and bridge messages.
(() => {
  const prefix = '__ATLAS_QA__';
  const header = 'x-atlas-qa-request';
  const doc = Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
  let sequence = 0, active = null;
  const ports = new WeakMap(), responses = new WeakMap();
  const emit = (kind, data = {}) => console.debug(prefix + JSON.stringify({
    kind, doc, seq: ++sequence, time_ms: performance.timeOrigin + performance.now(), ...data
  }));
  const bytes = value => {
    const a = value instanceof ArrayBuffer ? new Uint8Array(value) : value;
    let s = '';
    for (let i = 0; i < a.length; i += 8192) s += String.fromCharCode(...a.subarray(i, i + 8192));
    return btoa(s);
  };
  const state = port => {
    if (!ports.has(port)) ports.set(port, new Map());
    return ports.get(port);
  };
  const observe = (port, msg, direction) => {
    if (!msg || !String(msg.type).startsWith('ouro-widget-')) return null;
    const ids = state(port);
    if (msg.type === 'ouro-widget-fetch') {
      const id = new Headers(msg.init?.headers || {}).get(header);
      if (id) ids.set(msg.id, id);
    }
    const id = ids.get(msg.id) || null;
    emit('bridge', {id, bridge_id: msg.id, type: msg.type, phase: msg.phase,
      direction, data: msg.chunk ? bytes(msg.chunk) : undefined});
    return {id, phase: msg.phase};
  };
  const post = MessagePort.prototype.postMessage;
  MessagePort.prototype.postMessage = function(msg, ...rest) {
    // One diagnostic header crosses the existing bridge and native request unchanged.
    // It is the identity join; repeated URLs or equal payloads are never identities.
    if (msg?.type === 'ouro-widget-fetch' && String(msg.url).includes('/api/extensions/memory-atlas/')) {
      const headers = new Headers(msg.init?.headers || {});
      headers.set(header, doc + ':' + (++sequence));
      msg = {...msg, init: {...msg.init, headers: Array.from(headers)}};
    }
    observe(this, msg, 'send');
    return post.call(this, msg, ...rest);
  };
  const onmessage = Object.getOwnPropertyDescriptor(MessagePort.prototype, 'onmessage');
  Object.defineProperty(MessagePort.prototype, 'onmessage', {
    ...onmessage,
    set(handler) {
      onmessage.set.call(this, typeof handler !== 'function' ? handler : function(event) {
        const previous = active;
        active = observe(this, event.data, 'receive');
        try { return handler.call(this, event); } finally { active = previous; }
      });
    }
  });
  const close = MessagePort.prototype.close;
  MessagePort.prototype.close = function(...args) {
    emit('port_close', {ids: Array.from(state(this).values())});
    return close.apply(this, args);
  };
  const NativeResponse = Response;
  globalThis.Response = new Proxy(NativeResponse, {
    construct(target, args, newTarget) {
      const response = Reflect.construct(target, args, newTarget);
      if (active?.id && active.phase === 'headers') responses.set(response, active.id);
      return response;
    }
  });
  const json = NativeResponse.prototype.json;
  NativeResponse.prototype.json = function(...args) {
    const id = responses.get(this);
    if (!id) return json.apply(this, args);
    emit('consumer_json_start', {id});
    return json.apply(this, args).then(value => {
      emit('consumer_json_complete', {id, contract: value?.contract, body_error: !!value?.error});
      return value;
    }, error => { emit('consumer_json_error', {id, error: String(error)}); throw error; });
  };
  const fetch = globalThis.fetch;
  globalThis.fetch = function(input, init = {}) {
    const id = new Headers(init.headers || (input instanceof Request ? input.headers : {})).get(header);
    if (!id) return fetch.call(this, input, init);
    emit('native_fetch', {id, url: String(input)});
    init.signal?.addEventListener('abort', () => emit('native_abort', {id,
      reason: String(init.signal.reason), stack: new Error().stack}), {once: true});
    return fetch.call(this, input, init).then(response => {
      emit('native_response', {id, status: response.status});
      if (response.body) {
        const getReader = response.body.getReader;
        response.body.getReader = function(...args) {
          const reader = getReader.apply(this, args), read = reader.read;
          reader.read = function(...args) {
            return read.apply(this, args).then(part => {
              emit(part.done ? 'native_eof' : 'native_chunk', {id,
                data: part.done ? undefined : bytes(part.value)});
              return part;
            }, error => { emit('native_read_error', {id, error: String(error)}); throw error; });
          };
          return reader;
        };
      }
      return response;
    }, error => { emit('native_fetch_error', {id, error: String(error)}); throw error; });
  };
  addEventListener('pagehide', () => emit('pagehide'));
  emit('observer_ready');
})();
