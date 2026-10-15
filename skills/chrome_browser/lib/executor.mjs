// The task's one browser executor: the lease, the invocation queue, the
// persistent script realm and the guard questions. See PROTOCOL.md.

import crypto from 'node:crypto';
import vm from 'node:vm';

import {
  INPUT_OPERATIONS, NAVIGATION_OPERATIONS, OPERATIONS, READ_OPERATIONS, UNSUPPORTED_OPERATIONS,
} from '../extension/bridge_protocol.mjs';
import { askGuard } from './guard.mjs';
import { Listener, packageBuild, readPairing, stateDir } from './pairing.mjs';

export const PROTOCOL = 'ouroboros_chrome';
export const VERSION = 1;
export const EXECUTOR_OPERATIONS = ['sequence', 'cached_text'];
const PAIR_WAIT_MS = 40000;
const SEQUENCE_LIMIT = 32;
const CACHE_KEYS = 32;
const CACHE_CHARS = 2000000;
const SHORTHAND = { navigate: 'url', goto: 'url', open_url: 'url', press: 'key', cached_text: 'cache_key' };

export class BrowserError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

const closed = () => new BrowserError('BROWSER_INVOCATION_CLOSED', 'this invocation has ended; its browser object no longer acts');
/** A refusal handed to a script that may never look at it: observed here, and still a rejection for whoever awaits it. */
const refusal = (error) => {
  const promise = Promise.reject(error);
  promise.catch(() => {});
  return promise;
};
const codeOf = (text) => (/^([A-Z][A-Z0-9_]+)/.exec(text) || [])[1] || 'BROWSER_POLICY_UNAVAILABLE';
const isRecord = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);
/** The host refused a document the page had already committed: the page shows it, the lease is gone. */
const documentRefusal = (refused, detail = '') => new BrowserError(refused.code,
  `the page committed ${refused.url} and the host refused that document (${refused.block}); the lease was released and the page was left on that document${detail}`);

export class Executor {
  constructor(env = process.env) {
    this.env = env;
    this.owner = { task_id: env.OUROBOROS_BROWSER_OWNER_TASK_ID || '', attempt: env.OUROBOROS_BROWSER_OWNER_ATTEMPT || '' };
    this.guardPath = env.OUROBOROS_BROWSER_GUARD || '';
    this.generation = crypto.randomInt(1, 2 ** 47);
    const timeout = Number(env.OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS);
    this.executeTimeout = Number.isFinite(timeout) && timeout > 0 ? Math.min(timeout, 600000) : 120000;

    this.listener = null;
    this.listenerStart = null;
    this.listenerStatus = 'idle';
    this.pairing = { credential_present: false, origin_pinned: false, port: null, detail: '' };
    this.ext = null;
    this.approved = new Set();

    this.lease = null;
    this.handoff = null;
    this.handoffEpoch = 0;  // invalidates a resume's saved handoff when disconnect wins
    this.closure = { revoked: false, detached: false };
    this.transportLost = false;
    this.lastClosure = null;
    this.leaseGeneration = 0;
    this.attaching = null;  // the attachment in flight: what the extension reports before its lease exists
    this.releasing = 0;  // release() calls waiting for the extension's answer

    this.rpcSeq = 0;
    this.pending = new Map();
    this.queue = Promise.resolve();
    this.active = null;
    this.guardChain = Promise.resolve();  // every guard question, one at a time
    this.cache = new Map();               // text kept for cached_text; survives navigation

    this.realm = vm.createContext({});
    this.vars = vm.runInContext('({})', this.realm);
  }

  // ------------------------------------------------------------- state

  contractState() {
    const lease = this.lease;
    return {
      protocol: PROTOCOL, version: VERSION, owner: { ...this.owner }, executor_generation: this.generation,
      connected: Boolean(lease),
      lease: lease ? {
        lease_id: lease.lease_id, generation: lease.generation, tab_id: lease.tab_id,
        document_id: lease.document_id, url: lease.url, guard_armed: true,
      } : null,
      closure: { ...this.closure },
    };
  }

  state() {
    const state = this.contractState();
    state.phase = this.lease ? 'connected' : this.handoff ? 'handoff' : (this.closure.revoked || this.transportLost) ? 'closed' : 'idle';
    state.pairing = {
      paired: Boolean(this.ext), port: this.pairing.port, listener: this.listenerStatus,
      credential_present: this.pairing.credential_present, origin_pinned: this.pairing.origin_pinned,
      failed_attempts: this.listener ? this.listener.failed : 0,
    };
    const refusal = (this.listener && this.listener.lastRefusal) || this.pairing.detail;
    if (refusal) state.pairing.detail = refusal;
    state.approved_tabs = Array.from(this.approved, (tab) => ({ tab_id: tab }));
    if (this.handoff) state.handoff = { tab_id: this.handoff.tab_id, previous_lease_id: this.handoff.previous_lease_id };
    state.transport_lost = this.transportLost;
    if (this.lastClosure) state.last_closure = this.lastClosure;
    return state;
  }

  requireHost() {
    if (this.env.OUROBOROS_BROWSER_PROTOCOL !== PROTOCOL)
      throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', 'OUROBOROS_BROWSER_PROTOCOL is not ouroboros_chrome');
    if (!this.owner.task_id || !this.owner.attempt)
      throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', 'the host did not name the owning task attempt');
  }

  // ------------------------------------------------------------- transport

  /**
   * Bound on the first tools/call; never during initialize or tools/list, and
   * never for a host that does not speak this protocol. Calls that arrive
   * together wait for the same start.
   */
  ensureListener() {
    this.requireHost();
    if (!this.listenerStart && !this.closure.revoked && !this.transportLost) {
      const start = this.startListener();
      const result = start.finally(() => {
        // Setup can finish after an earlier state call. Retry only when no
        // listener exists; a listening or busy port is never started twice.
        if (this.listenerStart === result && !this.listener && !this.ext) this.listenerStart = null;
      });
      this.listenerStart = result;
    }
    return this.listenerStart || Promise.resolve();
  }

  async startListener() {
    this.listenerStatus = 'unconfigured';
    let pairing;
    try {
      pairing = readPairing(stateDir(this.env));
    } catch (error) {
      this.pairing.detail = String(error.message || error);
      return;
    }
    if (!pairing) { this.pairing.detail = 'no pairing credential: run the setup script'; return; }
    this.pairing = { credential_present: true, origin_pinned: Boolean(pairing.pinned_origin), port: pairing.port, detail: '' };
    if (!pairing.pinned_origin) { this.pairing.detail = 'no extension has completed pairing: run the setup script'; return; }
    try {
      this.listener = new Listener({
        port: pairing.port, credential: pairing.credential, origin: pairing.pinned_origin, build: await packageBuild(),
        executorGeneration: this.generation, onPaired: (socket) => this.onPaired(socket),
      });
      this.listenerStatus = await this.listener.start();
    } catch (error) {
      this.listener = null;
      this.pairing.detail = String(error.message || error);
    }
  }

  onPaired(socket) {
    this.ext = socket;
    const ping = setInterval(() => this.sendExt({ t: 'ping' }), 20000);
    socket.on('message', (data) => {
      let message;
      try { message = JSON.parse(data.toString('utf8')); } catch (error) { return; }
      if (this.ext === socket && isRecord(message)) this.onExtMessage(message);
    });
    socket.on('close', () => {
      clearInterval(ping);
      if (this.ext !== socket) return;
      this.ext = null;
      this.approved.clear();
      for (const settle of this.pending.values()) settle({ transport_lost: true });
      this.pending.clear();
      if (this.lease || this.handoff || this.releasing) {
        // Nothing confirms a detach here: the extension detaches on its own, unobserved.
        const hadLease = Boolean(this.lease);
        this.lease = null;
        this.handoff = null;
        this.transportLost = true;
        this.closeActive();
        if (hadLease) this.lastClosure = { reason: 'transport_lost', debugger_detach_ack: false };
      }
    });
  }

  sendExt(message) {
    try {
      if (this.ext) this.ext.send(JSON.stringify(message));
    } catch (error) { /* the close handler reports the loss */ }
  }

  /** `onReply`, if given, runs in the turn the answer arrives, before any message that follows it is read. */
  rpc(message, timeoutMs, onReply = null) {
    return new Promise((resolve) => {
      if (!this.ext) { resolve({ transport_lost: true }); return; }
      const id = ++this.rpcSeq;
      const timer = setTimeout(() => { this.pending.delete(id); resolve({ timeout: true }); }, timeoutMs);
      this.pending.set(id, (reply) => {
        clearTimeout(timer);
        if (onReply) onReply(reply);
        resolve(reply);
      });
      this.sendExt({ ...message, id });
    });
  }

  onExtMessage(message) {
    switch (message.t) {
      case 'tabs':
        this.approved = new Set((Array.isArray(message.tabs) ? message.tabs : [])
          .map((tab) => String(tab && tab.tab_id)).filter((tab) => /^\d{1,16}$/.test(tab)));
        break;
      case 'attached': case 'step_result': case 'released': case 'closed': {
        const settle = this.pending.get(message.id);
        if (settle) { this.pending.delete(message.id); settle(message); }
        break;
      }
      case 'nav': this.onDocument(message); break;
      case 'url':
        // Same document: only the URL changes, on the lease object itself, so the lease stays current.
        if (this.isCurrent(message) && typeof message.url === 'string') this.lease.url = message.url;
        break;
      case 'request': this.onRequest(message); break;
      case 'lost': this.onLost(message); break;
      default: break;
    }
  }

  /**
   * Every guard question goes through this one queue. `build` runs when its
   * turn comes, so the facts are current and step ids reach the host strictly
   * increasing — primitives, requests and document changes alike.
   */
  guarded(build) {
    const turn = this.guardChain.then(async () => {
      const facts = build();
      return facts ? askGuard(this.guardPath, facts) : null;
    });
    this.guardChain = turn.catch(() => {});
    return turn;
  }

  /**
   * The barrier before anything is reported: resolves once every question has
   * its answer and its consequence — also one that joined the queue while this
   * was waiting (a document that committed meanwhile, a new attachment).
   */
  async settleGuard() {
    let chain;
    do {
      chain = this.guardChain;
      await chain;
    } while (chain !== this.guardChain);
  }

  /** Is `lease` (anything with lease_id and generation) the live lease? Identity of the object is not the test. */
  isCurrent(lease) {
    return Boolean(this.lease && lease && this.lease.lease_id === lease.lease_id && this.lease.generation === lease.generation);
  }

  /** The invocation a request or a document change belongs to: open until its receipt is built. */
  running() {
    return this.active && !this.active.closed ? this.active : null;
  }

  // The main frame committed another document. The host must witness the new
  // lease before anything else is asked or reported.
  onDocument(message) {
    const lease = this.lease;
    const previous = isRecord(message.previous) ? message.previous : {};
    if (!lease || lease.lease_id !== previous.lease_id || lease.generation !== previous.generation) return;
    if (!Number.isInteger(message.generation) || message.generation <= lease.generation
        || typeof message.lease_id !== 'string' || message.lease_id === lease.lease_id
        || typeof message.document_id !== 'string' || typeof message.url !== 'string') return;
    const next = { ...lease, lease_id: message.lease_id, generation: message.generation, document_id: message.document_id, url: message.url };
    this.lease = next;
    this.leaseGeneration = Math.max(this.leaseGeneration, next.generation);
    const previousLease = { lease_id: lease.lease_id, generation: lease.generation, document_id: lease.document_id };
    const state = this.contractState();
    const requestId = typeof message.request_id === 'string' ? message.request_id : null;
    // In the same queue as every other question: nothing later is asked, and no
    // result or state is returned, before the host has witnessed this lease.
    this.guardChain = this.guardChain.then(async () => {
      const invocation = this.running();
      const entry = { step_id: invocation ? ++invocation.stepSeq : null, operation: 'document', outcome: 'observed', url: next.url, request_id: requestId };
      if (invocation) invocation.steps.push(entry);
      const block = await askGuard(this.guardPath, {
        kind: 'document', invocation_id: invocation ? invocation.id : null, step_id: entry.step_id,
        causal_step_id: invocation ? invocation.currentStep : null, request_id: requestId, previous: previousLease, state,
      });
      if (!block) return;
      entry.error = { code: codeOf(block), message: block };
      // The commit already happened. The invocation it belongs to fails, whatever its steps returned.
      if (invocation && !invocation.documentFailure) invocation.documentFailure = { code: entry.error.code, url: next.url, block };
      // Also when the page has moved on meanwhile: a later document of this attachment does not outlive the refusal.
      if (this.lease && this.lease.attached === next.attached) await this.release('navigation_refused', block);
    }).catch(() => {});
  }

  async onRequest(message) {
    const request = isRecord(message.request) ? message.request : {};
    let invocation = null;
    let block;
    try {
      block = await this.guarded(() => {
        const lease = this.lease;
        if (!lease || lease.lease_id !== message.lease_id || lease.generation !== message.generation) return null;
        invocation = this.running();
        return {
          kind: 'request', invocation_id: invocation ? invocation.id : null, step_id: invocation ? ++invocation.stepSeq : null,
          causal_step_id: invocation ? invocation.currentStep : null,
          request_id: request.request_id, url: request.url, method: request.method,
          post_data: request.post_data === undefined ? null : request.post_data, complete: request.complete === true,
          has_post_data: typeof request.has_post_data === 'boolean' ? request.has_post_data : null,
          resource_type: request.resource_type, is_main_frame: request.is_main_frame === true, target_tab_id: lease.tab_id,
          state: this.contractState(),
        };
      });
    } catch (error) {
      block = 'BROWSER_POLICY_UNAVAILABLE: the request could not be judged';
    }
    if (block === null) block = 'BROWSER_LEASE_CHANGED: the request belongs to another lease';
    if (block && invocation && invocation.blocked.length < 20)
      invocation.blocked.push(`${block} — ${request.method} ${String(request.url).slice(0, 300)}`);
    this.sendExt({ t: 'verdict', request_id: request.request_id, block });
  }

  onLost(message) {
    const tab = String(message.tab_id);
    const pending = this.attaching;
    if (pending && !pending.lease) {
      // No lease yet. Kept by tab — for an opened URL the tab is known from the answer, or from a `lost` that
      // names the lease being made — and an owner's revocation of the tab being attached is final from this
      // moment, whatever that answer says, also when the socket closes before it.
      if (pending.tab_id === null && message.lease_id === pending.lease_id) pending.tab_id = tab;
      pending.lost.set(tab, message);
      if (pending.tab_id === tab && (message.reason === 'owner_revoked' || message.reason === 'canceled_by_user')) this.closure.revoked = true;
    }
    if (!(this.lease && this.lease.tab_id === tab) && !(this.handoff && this.handoff.tab_id === tab)) return;
    this.lease = null;
    this.handoff = null;
    this.closeActive();
    if (message.reason === 'owner_revoked' || message.reason === 'canceled_by_user') this.closure.revoked = true;
    this.closure.detached = message.detached === true;
    this.lastClosure = { reason: String(message.reason), debugger_detach_ack: message.detached === true };
  }

  // ------------------------------------------------------------- lease

  usableLease() {
    if (this.closure.revoked) throw new BrowserError('BROWSER_SESSION_REVOKED', 'the owner ended this browser session');
    if (this.transportLost) throw new BrowserError('BROWSER_SESSION_DETACHED', 'the connection to the extension was lost');
    if (this.handoff) throw new BrowserError('BROWSER_HANDOFF_ACTIVE', 'the tab is handed to the owner; resume first');
    if (!this.lease) throw new BrowserError('BROWSER_SESSION_DETACHED', 'no connected lease');
    return this.lease;
  }

  async waitPaired() {
    const deadline = Date.now() + PAIR_WAIT_MS;
    while (!this.ext && this.listenerStatus === 'listening' && Date.now() < deadline)
      await new Promise((resolve) => setTimeout(resolve, 100));
    if (this.ext) return;
    if (this.listenerStatus === 'busy')
      throw new BrowserError('BROWSER_PORT_BUSY', `port ${this.pairing.port} is held by another process, probably another task's browser`);
    throw new BrowserError('BROWSER_NOT_PAIRED', this.pairing.detail
      || 'the paired extension did not connect; Chrome must be running with the extension enabled (it retries every 30 s)');
  }

  async attach(target, invocation, operation, outerArgs) {
    if (this.attaching) throw new BrowserError('BROWSER_LEASE_CHANGED', 'another connection is being made');
    if (!this.guardPath) throw new BrowserError('BROWSER_REQUEST_GUARD_UNAVAILABLE', 'the host gave no guard socket; nothing is attached without it');
    const leaseId = `l${crypto.randomBytes(8).toString('hex')}`;
    const generation = ++this.leaseGeneration;
    const pending = {
      tab_id: typeof target.tab_id === 'string' ? target.tab_id : null, lease_id: leaseId, lost: new Map(), lease: null, question: null, fault: '',
      handoffEpoch: this.handoffEpoch,
    };
    this.attaching = pending;
    // Runs in the turn the answer arrives, so a `lost` or `nav` read right after it already finds the lease, and
    // the host's connect question is queued ahead of any document question of that lease.
    const adopt = (reply) => {
      if (reply.ok !== true) return;
      if (operation === 'resume' && this.handoffEpoch !== pending.handoffEpoch) {
        pending.fault = 'the handoff ended during attachment';
        return;
      }
      if (pending.lost.has(String(reply.tab_id)) || this.closure.revoked) return;  // ended while pending: a late success is not adopted
      if (reply.lease_id !== leaseId || reply.generation !== generation) { pending.fault = 'the extension answered for another lease'; return; }
      if (reply.guard_armed !== true || !reply.document_id || !reply.url) { pending.fault = 'the extension reported an unarmed or incomplete attachment'; return; }
      const lease = {
        lease_id: leaseId, generation, attached: generation, tab_id: String(reply.tab_id), document_id: String(reply.document_id),
        url: String(reply.url), created: reply.created === true,
      };
      pending.lease = lease;
      this.lease = lease;
      this.handoff = null;
      this.closure.detached = false;
      // The host sees the newly armed lease before any request or document of it is judged.
      const state = this.contractState();
      pending.question = this.guarded(() => {
        if (!this.lease || this.lease.attached !== generation) return null;  // gone before its turn: nothing to witness
        invocation.currentStep = ++invocation.stepSeq;
        return { kind: 'primitive', invocation_id: invocation.id, step_id: invocation.currentStep, operation, args: outerArgs, state };
      });
    };
    try {
      const reply = await this.rpc({ t: 'attach', lease_id: leaseId, generation, ...target }, 30000, adopt);
      if (reply.transport_lost) throw new BrowserError('BROWSER_SESSION_DETACHED', 'the connection to the extension closed');
      if (reply.timeout) {
        // Whether Chrome attached is unknown. Dropping the socket makes the extension release whatever it holds.
        this.transportLost = true;
        if (this.ext) this.ext.terminate();
        throw new BrowserError('BROWSER_ACTION_OUTCOME_UNKNOWN', 'the extension did not confirm the attachment');
      }
      if (!pending.lease) {
        const tab = reply.tab_id !== undefined && reply.tab_id !== null ? String(reply.tab_id) : pending.tab_id;
        const lost = tab === null ? undefined : pending.lost.get(tab);
        const reason = lost ? String(lost.reason) : (!reply.ok && typeof reply.reason === 'string' ? reply.reason : '');
        if (reason || (reply.ok && this.closure.revoked)) {
          // The owner, or Chrome, ended it while it was being made. What is detached is what the extension says last.
          const ended = reason || 'owner_revoked';
          const owner = ended === 'owner_revoked' || ended === 'canceled_by_user';
          if (owner) this.closure.revoked = true;
          this.handoff = null;
          if (reply.ok) {
            await this.releaseTab(tab, 'refused', '', ended);  // it reported an attachment: only a release says it is gone
          } else {
            const detached = typeof reply.detached === 'boolean' ? reply.detached : Boolean(lost && lost.detached === true);
            this.closure.detached = detached;
            this.lastClosure = { reason: ended, debugger_detach_ack: detached };
          }
          const failure = owner
            ? new BrowserError('BROWSER_SESSION_REVOKED', 'the owner ended this browser session while the tab was being attached')
            : new BrowserError('BROWSER_SESSION_DETACHED', `the tab was lost while it was being attached (${ended})`);
          failure.ended = true;
          throw failure;
        }
        if (!reply.ok) throw new BrowserError((reply.error && reply.error.code) || 'BROWSER_INTERNAL', (reply.error && reply.error.message) || 'attach failed');
        const unarmed = /unarmed/.test(pending.fault);
        await this.releaseTab(tab, 'refused', pending.fault);
        throw new BrowserError(unarmed ? 'BROWSER_REQUEST_GUARD_UNAVAILABLE' : 'BROWSER_LEASE_CHANGED', unarmed ? 'request interception was not armed' : pending.fault);
      }
      const block = await pending.question;
      if (block) {
        // Whatever document the tab has reached meanwhile: a refused connection leaves no lease behind.
        if (this.lease && this.lease.attached === generation) await this.release('refused', block);
        throw new BrowserError(codeOf(block), block);
      }
      if (!this.lease && !invocation.documentFailure) {
        throw this.closure.revoked
          ? new BrowserError('BROWSER_SESSION_REVOKED', 'the owner ended this browser session while it was being connected')
          : new BrowserError('BROWSER_SESSION_DETACHED', 'the lease ended while it was being connected');
      }
    } finally {
      this.attaching = null;
    }
  }

  /** Ends the current lease. Returns whether the extension confirmed the detach. */
  async release(reason, detail = '') {
    const lease = this.lease;
    if (!lease) return false;
    this.lease = null;
    this.closeActive();
    return this.releaseTab(lease.tab_id, reason, detail);
  }

  /** Sends `release` for a tab in this very turn and records what the extension confirmed. */
  async releaseTab(tab, reason, detail = '', recorded = reason) {
    // Counted until answered: a transport that closes meanwhile is a loss, not an idle executor.
    this.releasing += 1;
    let reply;
    try {
      reply = await this.rpc({ t: 'release', tab_id: tab, reason }, 5000);
    } finally {
      this.releasing -= 1;
    }
    const acknowledged = reply.detached === true;
    this.closure.detached = acknowledged;
    this.lastClosure = { reason: recorded, debugger_detach_ack: acknowledged };
    const text = detail || reply.error || '';
    if (text) this.lastClosure.detail = String(text).slice(0, 400);
    return acknowledged;
  }

  // ------------------------------------------------------------- control tools

  /** connect and resume are invocations too: their own question and the first requests share one id. */
  control(invocationId, run) {
    this.requireHost();
    if (typeof invocationId !== 'string' || !invocationId)
      throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', 'the host did not supply _meta.ouroboros_invocation_id');
    return this.enqueue(async () => {
      const invocation = this.open(invocationId);
      try {
        await run(invocation);
      } finally {
        await this.finish(invocation);
      }
      if (invocation.documentFailure) throw documentRefusal(invocation.documentFailure);
    });
  }

  connect(args, invocationId) {
    return this.control(invocationId, async (invocation) => {
      const keys = isRecord(args) ? Object.keys(args) : [];
      if (keys.length !== 1 || !['tab_id', 'url'].includes(keys[0]) || typeof args[keys[0]] !== 'string' || !args[keys[0]])
        throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', 'connect takes exactly one of tab_id or url');
      if (this.closure.revoked) throw new BrowserError('BROWSER_SESSION_REVOKED', 'the owner ended this browser session');
      if (this.transportLost) throw new BrowserError('BROWSER_SESSION_DETACHED', 'the connection to the extension was lost');
      if (this.handoff) throw new BrowserError('BROWSER_HANDOFF_ACTIVE', 'a tab is handed to the owner; resume it first');
      if (this.lease) throw new BrowserError('BROWSER_LEASE_CHANGED', 'already connected; disconnect first');
      await this.ensureListener();
      await this.waitPaired();
      if ('tab_id' in args && !this.approved.has(args.tab_id))
        throw new BrowserError('BROWSER_TAB_NOT_APPROVED', 'the owner has not shared a tab with this id');
      await this.attach(args, invocation, 'connect', args);
    });
  }

  async handoffToOwner() {
    this.requireHost();
    const lease = this.lease;
    if (!lease) throw new BrowserError('BROWSER_SESSION_DETACHED', 'no connected lease');
    this.handoffEpoch += 1;
    this.handoff = { tab_id: lease.tab_id, previous_lease_id: lease.lease_id };
    // release() clears the lease and sends the message before its first await.
    await this.release('handoff');
  }

  resume(invocationId) {
    return this.control(invocationId, async (invocation) => {
      if (this.closure.revoked) throw new BrowserError('BROWSER_SESSION_REVOKED', 'the owner ended this browser session');
      if (!this.handoff) throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', 'no tab is handed off');
      if (!this.ext) throw new BrowserError('BROWSER_SESSION_DETACHED', 'the connection to the extension was lost');
      const handoff = this.handoff;
      const handoffEpoch = this.handoffEpoch;
      try {
        await this.attach({ tab_id: handoff.tab_id }, invocation, 'resume', {});
      } catch (error) {
        // The extension no longer knows the tab (the owner closed it): the handoff is over, not pending.
        const gone = error.code === 'BROWSER_TAB_NOT_APPROVED';
        if (gone && this.handoff === handoff) {
          this.handoff = null;
          this.lastClosure = { reason: 'target_closed', debugger_detach_ack: this.closure.detached };
        } else if (this.handoffEpoch === handoffEpoch && (!this.handoff || this.handoff === handoff)
          && !this.lease && !this.closure.revoked && !this.transportLost && !gone && !error.ended) this.handoff = handoff;
        throw error;
      }
    });
  }

  async disconnect() {
    this.requireHost();
    this.handoffEpoch += 1;
    if (!this.lease && this.handoff) {
      // Ends a handoff without attaching: the tab stays with the owner, and the extension stops offering it for resume.
      const { tab_id: tab } = this.handoff;
      this.handoff = null;
      const reply = await this.rpc({ t: 'release', tab_id: tab, reason: 'disconnect' }, 5000);
      this.lastClosure = { reason: 'disconnect', debugger_detach_ack: this.closure.detached && reply.detached === true };
      return;
    }
    if (!this.lease) throw new BrowserError('BROWSER_SESSION_DETACHED', 'no connected lease');
    await this.release('disconnect');
  }

  /** stdio EOF: ask for the detach, wait briefly, and say whether it was confirmed. */
  async shutdown() {
    let acknowledged = null;
    if (this.lease) {
      acknowledged = await Promise.race([this.release('shutdown'), new Promise((resolve) => setTimeout(() => resolve(false), 1500))]);
    }
    if (this.listener) this.listener.close();
    return acknowledged;
  }

  // ------------------------------------------------------------- invocations

  /** Synchronous and out of band: from here the invocation sends nothing, on either side. */
  closeActive() {
    const invocation = this.active;
    if (!invocation || invocation.closing) return;
    invocation.closing = true;
    invocation.end();
    for (const cancel of Array.from(invocation.sleeps)) cancel();
    invocation.closeAck = this.ext ? this.rpc({ t: 'close', invocation_id: invocation.id }, 5000) : null;
  }

  enqueue(run) {
    const result = this.queue.then(run, run);
    this.queue = result.catch(() => {});
    return result;
  }

  open(invocationId) {
    const invocation = {
      id: invocationId, steps: [], stepSeq: 0, currentStep: null, pending: new Set(), sleeps: new Set(),
      closing: false, closed: false, closeAck: null, chain: Promise.resolve(), logs: [], images: [], blocked: [],
    };
    // Resolves when the invocation is closed, by whatever closes it; never rejects.
    invocation.ended = new Promise((resolve) => { invocation.end = resolve; });
    this.active = invocation;
    this.sendExt({ t: 'open', invocation_id: invocationId });
    return invocation;
  }

  /**
   * Close, on return, error and timeout alike: refuse new steps here; tell the
   * extension, which kills its queued steps of this invocation and stops the
   * running one at its next command; collect their answers; let the host
   * witness any document change. Only then is the receipt built.
   */
  async finish(invocation) {
    if (this.active === invocation) this.closeActive();
    else if (!invocation.closing) { invocation.closing = true; invocation.end(); }
    while (invocation.pending.size) await Promise.allSettled(Array.from(invocation.pending));
    if (invocation.closeAck) await invocation.closeAck;
    await this.settleGuard();
    invocation.closed = true;
    if (this.active === invocation) this.active = null;
  }

  async invoke(invocationId, body) {
    this.requireHost();
    if (typeof invocationId !== 'string' || !invocationId)
      throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', 'the host did not supply _meta.ouroboros_invocation_id');
    return this.enqueue(async () => {
      const invocation = this.open(invocationId);
      let result;
      let failure = null;
      try {
        result = await body(invocation);
      } catch (error) {
        failure = error;
      }
      await this.finish(invocation);
      if (!failure && invocation.documentFailure) {
        failure = documentRefusal(invocation.documentFailure);
        if (result !== undefined) failure.partial = result;
      }

      const receipt = { ok: !failure, invocation_id: invocation.id };
      if (failure) {
        receipt.error = {
          code: failure instanceof BrowserError ? failure.code : 'BROWSER_SCRIPT_ERROR',
          message: String((failure && failure.message) || failure).slice(0, 2000),
        };
        // Whatever was returned before the failure, also 0, false and null.
        if (typeof failure === 'object' && failure !== null && 'partial' in failure) receipt.partial_result = failure.partial;
      } else {
        receipt.result = result === undefined ? null : result;
      }
      receipt.logs = invocation.logs;
      receipt.steps = invocation.steps;
      if (invocation.blocked.length) receipt.blocked_requests = invocation.blocked;
      receipt.state = this.contractState();
      let text;
      try {
        text = JSON.stringify(receipt);
      } catch (error) {
        // Something the script returned is not JSON (a cycle, a BigInt). The steps, the error and the state always are.
        const isJson = (value) => { try { JSON.stringify(value); return true; } catch (unserializable) { return false; } };
        if ('partial_result' in receipt && !isJson(receipt.partial_result)) {
          delete receipt.partial_result;
          receipt.error.message = `${receipt.error.message} (the partial result is not JSON and was left out)`;
        }
        if ('result' in receipt && !isJson(receipt.result)) {
          receipt.result = null;
          receipt.ok = false;
          receipt.error = { code: 'BROWSER_SCRIPT_ERROR', message: 'the script result is not JSON' };
        }
        text = JSON.stringify(receipt);
      }
      return { text, images: invocation.images, isError: !receipt.ok };
    });
  }

  /** One at a time within an invocation, in call order, whatever the script awaits. */
  primitive(invocation, operation, rawArgs) {
    if (invocation.closing) return refusal(closed());
    const turn = invocation.chain.then(() => this.runPrimitive(invocation, operation, rawArgs));
    invocation.chain = turn.catch(() => {});
    return turn;
  }

  async runPrimitive(invocation, operation, rawArgs) {
    if (invocation.closing) throw closed();
    if (typeof operation !== 'string' || !isRecord(rawArgs))
      throw new BrowserError('BROWSER_ARGUMENT_INVALID', 'an operation takes one arguments object');
    const args = JSON.parse(JSON.stringify(rawArgs));
    const local = operation === 'cached_text';
    const step = { step_id: null, operation, outcome: 'not_dispatched' };
    let lease = null;
    try {
      const block = await this.guarded(() => {
        if (invocation.closing) throw closed();  // closed while it waited its turn: nothing is asked
        step.step_id = ++invocation.stepSeq;
        invocation.currentStep = step.step_id;
        invocation.steps.push(step);
        if (UNSUPPORTED_OPERATIONS.includes(operation))
          throw new BrowserError('BROWSER_OPERATION_UNSUPPORTED', `${operation} is not implemented in this version`);
        if (!local && !OPERATIONS.includes(operation)) throw new BrowserError('BROWSER_OPERATION_UNSUPPORTED', `unknown operation ${operation}`);
        if (!local) lease = this.usableLease();
        return { kind: 'primitive', invocation_id: invocation.id, step_id: step.step_id, operation, args, state: this.contractState() };
      });
      if (block) throw new BrowserError(codeOf(block), block);
      if (invocation.closing) throw closed();
      if (local) {
        step.outcome = 'observed';
        return this.cachedText(args);
      }
      if (!this.isCurrent(lease)) throw new BrowserError('BROWSER_LEASE_CHANGED', 'the lease changed while the host was deciding');

      const textRead = operation === 'read' || operation === 'get_text';
      let wireArgs = args;
      let outputChars = 0;
      if (textRead && (args.start !== undefined || args.capture_chars !== undefined)) {
        const start = args.start === undefined ? 0 : args.start;
        if (!Number.isSafeInteger(start) || start < 0)
          throw new BrowserError('BROWSER_ARGUMENT_INVALID', 'start must be a nonnegative integer');
        outputChars = Math.min(Math.max(Number(args.max_chars) || 20000, 1), 200000);
        const capture = args.capture_chars === undefined ? outputChars : args.capture_chars;
        if (!Number.isSafeInteger(capture) || capture < outputChars || capture > 200000)
          throw new BrowserError('BROWSER_ARGUMENT_INVALID', 'capture_chars must be an integer from max_chars through 200000');
        if (args.capture_chars !== undefined && !/^[\w.-]{1,64}$/.test(args.cache_key || ''))
          throw new BrowserError('BROWSER_ARGUMENT_INVALID', 'capture_chars requires a valid cache_key');
        wireArgs = { ...args, start, max_chars: capture };
      }

      // Admitted. From here the step is settled before the invocation's receipt.
      let budget = 20000;
      if (typeof args.timeout_ms === 'number' && args.timeout_ms > 0) budget += Math.min(args.timeout_ms, 60000);
      if (INPUT_OPERATIONS.includes(operation) || NAVIGATION_OPERATIONS.includes(operation)) budget += 15000;
      let settled;
      const admitted = new Promise((resolve) => { settled = resolve; });
      invocation.pending.add(admitted);
      let reply;
      try {
        reply = await this.rpc({
          t: 'step', lease_id: lease.lease_id, generation: lease.generation, invocation_id: invocation.id, step_id: step.step_id,
          operation, args: wireArgs,
        }, budget);
        await this.settleGuard();  // a document change this step caused is witnessed before its result is returned
      } finally {
        invocation.pending.delete(admitted);
        settled();
      }
      if (reply.transport_lost || reply.timeout || reply.lease_id !== lease.lease_id || reply.generation !== lease.generation) {
        step.outcome = 'unknown';
        throw new BrowserError('BROWSER_ACTION_OUTCOME_UNKNOWN', reply.transport_lost
          ? 'the connection to the extension closed before an answer; the step is not repeated'
          : reply.timeout ? 'the extension did not answer in time; the step is not repeated' : 'the answer was for another lease');
      }
      step.outcome = String(reply.outcome);
      // A document the host refused after this step was admitted ends it with an error; its outcome stays as Chrome reported it.
      const refused = invocation.documentFailure;
      if (!reply.ok || refused) {
        const code = reply.ok ? 'OK' : (reply.error && reply.error.code) || 'BROWSER_INTERNAL';
        const message = reply.ok ? 'the step itself completed' : (reply.error && reply.error.message) || 'step failed';
        const also = refused ? `; the host refused the document the page committed (${refused.url})` : '';
        // One code for every "may have happened": the caller must read the page, not send it again.
        if (step.outcome === 'unknown') throw new BrowserError('BROWSER_ACTION_OUTCOME_UNKNOWN', `the step stopped part-way (${code}: ${message})${also}; it is not repeated`);
        if (refused) throw documentRefusal(refused, `; this step's outcome is ${step.outcome} (${code}: ${message}) and it is not repeated`);
        throw new BrowserError(code, message);
      }
      let value = reply.value;
      if (operation === 'screenshot' && isRecord(value) && typeof value.data === 'string') {
        invocation.images.push({ mimeType: value.mime, data: value.data });
        value = { mime: value.mime, bytes: Math.floor(value.data.length * 3 / 4), image: invocation.images.length - 1, clipped: value.clipped };
      }
      if (typeof args.cache_key === 'string' && READ_OPERATIONS.includes(operation) && operation !== 'screenshot' && isRecord(value))
        value.cached = this.remember(args.cache_key, value, {
          observation_id: `${invocation.id}:${step.step_id}`, operation,
          executor_generation: this.generation, lease_id: lease.lease_id,
          generation: lease.generation, tab_id: lease.tab_id, document_id: lease.document_id, url: lease.url,
        });
      if (textRead && args.capture_chars !== undefined && isRecord(value) && typeof value.text === 'string') {
        value = { ...value, text: value.text.slice(0, outputChars) };
        const start = value.coverage?.start ?? (args.start || 0);
        value.coverage = { start, end: start + value.text.length, total_length: value.length,
          complete: start === 0 && start + value.text.length === value.length };
        value.truncated = start + value.text.length < value.length;
      }
      return value === undefined ? null : value;
    } catch (error) {
      step.error = { code: error.code || 'BROWSER_INTERNAL', message: String(error.message || error).slice(0, 600) };
      throw error;
    }
  }

  /** Keeps what a read returned, as text, for cached_text. Not tied to a document. */
  remember(key, value, source = null) {
    if (!/^[\w.-]{1,64}$/.test(key)) return { stored: false, reason: 'cache_key must be 1-64 letters, digits, dot, dash or underscore' };
    const text = typeof value.text === 'string' ? value.text : typeof value.outline === 'string' ? value.outline : JSON.stringify(value);
    if (text.length > CACHE_CHARS) return { stored: false, reason: 'too large to keep' };
    const coverage = typeof value.text === 'string'
      ? (value.coverage ? { ...value.coverage } : { start: 0, end: text.length, total_length: Number.isInteger(value.length) ? value.length : text.length,
        complete: !Number.isInteger(value.length) || text.length === value.length }) : null;
    this.cache.delete(key);
    if (this.cache.size >= CACHE_KEYS) this.cache.delete(this.cache.keys().next().value);
    this.cache.set(key, coverage ? { text, coverage, source: source ? { ...source } : null } : text);
    return { stored: true, cache_key: key, length: text.length, ...(coverage ? { coverage: { ...coverage }, source: source ? { ...source } : null } : {}) };
  }

  cachedText(args) {
    const entry = this.cache.get(args.cache_key);
    if (entry === undefined) throw new BrowserError('BROWSER_ARGUMENT_INVALID', 'nothing is cached under this cache_key');
    const historical = typeof entry === 'string';
    const text = historical ? entry : entry.text;
    const coverage = historical
      ? { start: 0, end: text.length, total_length: text.length, complete: true } : { ...entry.coverage };
    const source = historical ? null : entry.source;
    const max = Math.min(Math.max(Number(args.max_chars) || 20000, 1), 200000);
    if (typeof args.find === 'string' && args.find) {
      const lines = text.split('\n');
      const matches = [];
      lines.forEach((line, index) => { if (matches.length < 50 && line.includes(args.find)) matches.push({ line: index + 1, text: line.slice(0, 400) }); });
      return { cache_key: args.cache_key, length: text.length, find: args.find, matches,
        coverage, complete: coverage.complete, ...(source ? { source: { ...source } } : {}) };
    }
    const start = historical ? Math.min(Math.max(Number(args.start) || 0, 0), text.length)
      : args.start === undefined ? coverage.start : args.start;
    if (historical) return { cache_key: args.cache_key, length: text.length, start,
      text: text.slice(start, start + max), truncated: start + max < text.length, coverage, complete: true };
    if (!Number.isSafeInteger(start) || start < coverage.start || start > coverage.end
      || (start === coverage.end && coverage.end < coverage.total_length))
      throw new BrowserError('BROWSER_CACHE_MISS', `start is outside captured coverage ${coverage.start}..${coverage.end}; read the missing window from the page`);
    const slice = text.slice(start - coverage.start, start - coverage.start + max);
    return { cache_key: args.cache_key, length: text.length, start, text: slice,
      truncated: start + slice.length < coverage.total_length, coverage, complete: coverage.complete,
      ...(source ? { source: { ...source } } : {}) };
  }

  /** A finite list of typed steps in one invocation; it stops at the first step that fails. */
  async sequence(invocation, args) {
    const steps = isRecord(args) ? args.steps : null;
    if (!Array.isArray(steps) || !steps.length || steps.length > SEQUENCE_LIMIT
        || !steps.every((step) => isRecord(step) && typeof step.operation === 'string' && step.operation !== 'sequence' && isRecord(step.args)))
      throw new BrowserError('BROWSER_ARGUMENT_INVALID', `sequence takes 1 to ${SEQUENCE_LIMIT} steps of {operation, args}`);
    const results = [];
    for (const [index, step] of steps.entries()) {
      try {
        results.push({ ok: true, value: await this.primitive(invocation, step.operation, step.args) });
      } catch (error) {
        const failure = new BrowserError(error.code || 'BROWSER_INTERNAL', `step ${index + 1} of ${steps.length} (${step.operation}): ${error.message}`);
        failure.partial = results;
        throw failure;
      }
    }
    return results;
  }

  action(operation, args, invocationId) {
    return this.invoke(invocationId, (invocation) => (operation === 'sequence'
      ? this.sequence(invocation, args) : this.primitive(invocation, operation, args)));
  }

  execute(code, invocationId) {
    return this.invoke(invocationId, async (invocation) => {
      let script;
      try {
        script = new vm.Script(`(async (browser, vars, console, sleep) => {\n${code}\n})`, { filename: 'browser_execute.js' })
          .runInContext(this.realm, { timeout: 5000 });
      } catch (error) {
        throw new BrowserError('BROWSER_SCRIPT_ERROR', String((error && error.message) || error));
      }
      // Bound to this invocation by closure, never a global a later call could reuse.
      const call = (operation) => (input = {}) => this.primitive(invocation, operation,
        typeof input === 'string' ? { [SHORTHAND[operation] || 'selector']: input } : input);
      // state() waits like browser_state does: it never shows a lease the host has not witnessed.
      const browser = {
        action: (operation, args = {}) => this.primitive(invocation, operation, args),
        state: async () => { await this.settleGuard(); return this.contractState(); },
      };
      for (const operation of [...OPERATIONS, ...UNSUPPORTED_OPERATIONS, 'cached_text']) browser[operation] = call(operation);
      const print = (...parts) => {
        if (invocation.logs.length < 200) {
          invocation.logs.push(parts.map((part) => {
            if (typeof part === 'string') return part;
            try { return JSON.stringify(part); } catch (error) { return String(part); }
          }).join(' ').slice(0, 2000));
        }
      };
      const sleep = (ms) => {
        const nap = new Promise((resolve, reject) => {
          if (invocation.closing) { reject(closed()); return; }
          const cancel = () => { clearTimeout(timer); invocation.sleeps.delete(cancel); reject(closed()); };
          const timer = setTimeout(() => { invocation.sleeps.delete(cancel); resolve(); }, Math.min(Math.max(Number(ms) || 0, 0), 30000));
          invocation.sleeps.add(cancel);
        });
        // Observed here: cancelling a nap the script never awaited must not end the process. One it awaits still rejects.
        nap.catch(() => {});
        return nap;
      };
      // A handoff, a disconnect, a revocation or a loss ends the call, also while the script waits on a promise
      // of its own that nothing here can settle. The steps it started answer first (they are being cut), and the
      // script has one turn to take those answers and return; a script still waiting then is cut. The script
      // cannot be stopped; its browser object is dead.
      const ended = invocation.ended.then(async () => {
        while (invocation.pending.size) await Promise.allSettled(Array.from(invocation.pending));
        await this.settleGuard();
        await new Promise((resolve) => setImmediate(resolve));
        throw invocation.documentFailure ? documentRefusal(invocation.documentFailure)
          : new BrowserError('BROWSER_INVOCATION_CLOSED', 'the invocation was closed before the script returned (handoff, disconnect, revocation or loss)');
      });
      let timer;
      const expiry = new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new BrowserError('BROWSER_EXECUTE_TIMEOUT', `the script ran longer than ${this.executeTimeout} ms`)), this.executeTimeout);
      });
      try {
        return await Promise.race([
          Promise.resolve().then(() => script(Object.freeze(browser), this.vars, { log: print, info: print, warn: print, error: print }, sleep)),
          expiry,
          ended,
        ]);
      } catch (error) {
        if (error instanceof BrowserError) throw error;
        throw new BrowserError('BROWSER_SCRIPT_ERROR', String((error && error.stack) || error));
      } finally {
        clearTimeout(timer);
      }
    });
  }
}
