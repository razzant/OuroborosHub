// Owner controls. Every effect here starts from a click in this popup.

const $ = (id) => document.getElementById(id);
let currentTab = null;
let shown = null;  // the state the owner last saw

async function activeTab() {
  // Only the id of the tab this popup was opened on; no title, no URL, no other tab.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab && Number.isInteger(tab.id) ? tab.id : null;
}

function render(state) {
  shown = state;
  $('error').textContent = state.ok === false ? state.message : (state.connected ? '' : state.error || '');
  $('summary').textContent = state.connected
    ? `Connected to an Ouroboros task (executor ${state.executor_generation}).`
    : state.has_credential ? 'Pairing saved. No Ouroboros task is connected right now.' : 'Not paired.';
  $('summary').className = state.connected ? 'on' : 'note';
  if (document.activeElement !== $('port')) $('port').value = state.port;
  const shared = currentTab != null && state.approved.includes(currentTab);
  const leased = state.lease && state.lease.tab_id === currentTab;
  $('tab').textContent = currentTab == null ? 'No tab.'
    : leased ? `Tab ${currentTab}: Ouroboros is attached${state.lease.created ? ' (it opened this tab)' : ''}.`
      : shared ? `Tab ${currentTab}: shared, not attached.` : `Tab ${currentTab}: not shared.`;
  $('share').disabled = !state.connected || shared || currentTab == null;
  $('stop').disabled = !(shared || leased);
  $('forget').disabled = !state.has_credential;
  $('connect').disabled = state.has_credential;
}

async function ask(message) {
  try {
    render(await chrome.runtime.sendMessage(message));
  } catch (error) {
    $('error').textContent = String(error.message || error);
  }
}

$('share').addEventListener('click', () => ask({ type: 'share', tab_id: currentTab }));
$('stop').addEventListener('click', () => ask({ type: 'stop', tab_id: currentTab }));
$('forget').addEventListener('click', () => ask({ type: 'forget' }));
$('connect').addEventListener('click', async () => {
  $('connect').disabled = true;
  // The pairing state this click was made on: a Forget or Save while the helper answers refuses its answer.
  const epoch = shown ? shown.pairing_epoch : null;
  let pairing;
  try {
    pairing = await chrome.runtime.sendNativeMessage('com.ouroboros.chrome_browser', { type: 'bootstrap', version: 1 });
  } catch (error) {
    $('error').textContent = `${String(error.message || error)} Ask Ouroboros to set up the local helper for this browser, or use Manual pairing below.`;
    $('connect').disabled = false;
    return;
  }
  if (!pairing || pairing.ok !== true) {
    $('error').textContent = String(pairing?.error || 'The local helper did not answer.');
    $('connect').disabled = false;
    return;
  }
  await ask({ type: 'bootstrap', credential: pairing.credential, port: pairing.port, epoch });
});
$('save').addEventListener('click', async () => {
  await ask({ type: 'save', credential: $('credential').value, port: Number($('port').value) });
  $('credential').value = '';
});

(async () => {
  currentTab = await activeTab();
  await ask({ type: 'status' });
})();
