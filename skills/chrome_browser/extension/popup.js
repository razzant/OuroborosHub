// Owner controls. Every effect here starts from a click in this popup.

const $ = (id) => document.getElementById(id);
let currentTab = null;

async function activeTab() {
  // Only the id of the tab this popup was opened on; no title, no URL, no other tab.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab && Number.isInteger(tab.id) ? tab.id : null;
}

function render(state) {
  $('error').textContent = state.ok === false ? state.message : (state.connected ? '' : state.error || '');
  $('summary').textContent = state.connected
    ? `Connected to an Ouroboros task (executor ${state.executor_generation}).`
    : state.has_credential ? 'Paired. No Ouroboros task is connected right now.' : 'Not paired.';
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
$('save').addEventListener('click', async () => {
  await ask({ type: 'save', credential: $('credential').value, port: Number($('port').value) });
  $('credential').value = '';
});

(async () => {
  currentTab = await activeTab();
  await ask({ type: 'status' });
})();
