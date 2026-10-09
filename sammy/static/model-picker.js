'use strict';

// One saved preference shared by Settings and the composer. Never carry overrides across models.
// `busy` disables the controls while loading or saving; only `saving` holds back a message, as the next run must use
// the choice being saved. Loading does not: the server reads the saved preference itself.
const modelPicker = { data: null, busy: false, saving: false, error: '', message: '', revision: 0 };
const MODEL_PICKERS = ['quick-model', 'settings-model'];
const SIMPLE_THINKING = ['low', 'medium', 'high'];

function resetModelPreferences() {
  modelPicker.revision += 1;
  Object.assign(modelPicker, { data: null, busy: false, saving: false, error: '', message: '' });
  renderModelPickers();
}

async function loadModelPreferences() {
  if (modelPicker.busy) return;
  const revision = ++modelPicker.revision;
  modelPicker.busy = true;
  modelPicker.error = '';
  modelPicker.message = 'Loading models…';
  renderModelPickers();
  try {
    const data = await api('/api/model-preferences');
    if (revision !== modelPicker.revision) return;
    modelPicker.data = data;
    modelPicker.message = '';
  } catch (error) {
    if (revision !== modelPicker.revision) return;
    if (error.name !== 'AbortError') modelPicker.error = `Could not load models. ${error.message}`;
    modelPicker.message = '';
  } finally {
    if (revision === modelPicker.revision) {
      modelPicker.busy = false;
      renderModelPickers();
    }
  }
}

async function saveModelPreferences(model, settings) {
  if (modelPicker.busy) return;
  const revision = ++modelPicker.revision;
  modelPicker.busy = true;
  modelPicker.saving = true;
  modelPicker.error = '';
  modelPicker.message = 'Saving model settings…';
  renderModelPickers();
  try {
    const data = await telemetry.span('save model preferences', { model }, () => (
      api('/api/model-preferences', { method: 'PUT', body: { model, settings } })));
    if (revision !== modelPicker.revision) return;
    modelPicker.data = data;
    modelPicker.message = 'Saved for your next run.';
  } catch (error) {
    if (revision !== modelPicker.revision) return;
    modelPicker.error = `Could not save. Your previous selection is still shown. ${error.message}`;
    modelPicker.message = '';
  } finally {
    if (revision === modelPicker.revision) {
      modelPicker.busy = false;
      modelPicker.saving = false;
      renderModelPickers();
    }
  }
}

function preferenceField(prefix, field, label, control) {
  const wrapper = element('div', '', 'model-field');
  control.id = `${prefix}-${field}`;
  const caption = element('label', label);
  caption.htmlFor = control.id;
  wrapper.append(caption, control);
  return wrapper;
}

function preferenceSelect(values, value, defaultLabel = null) {
  const select = element('select');
  if (defaultLabel !== null) select.append(new Option(defaultLabel, ''));
  for (const item of values) select.append(new Option(item, item));
  select.value = value == null ? '' : String(value);
  return select;
}

function changeModelSetting(field, value) {
  const { model, settings } = modelPicker.data;
  const next = { ...settings };
  if (value === '') delete next[field]; else next[field] = value;
  report(saveModelPreferences(model, next));
}

function settingSelect(prefix, model, field, values, label) {
  const current = modelPicker.data.settings[field];
  const fallback = model.defaults[field];
  const select = preferenceSelect(values, current, fallback == null ? 'Default' : `Default (${fallback})`);
  // A nonstandard thinking level is shown in Advanced, not silently presented as low/medium/high.
  if (current != null && !values.includes(String(current))) select.value = '';
  select.addEventListener('change', () => changeModelSetting(field, select.value));
  return preferenceField(prefix, field, label, select);
}

function renderModelPickers() {
  const focused = document.activeElement.id;
  for (const prefix of MODEL_PICKERS) {
    const root = $(prefix);
    const advancedOpen = Boolean(root.querySelector('details[open]'));
    const controls = element('fieldset', '', 'model-controls');
    controls.disabled = modelPicker.busy;
    const data = modelPicker.data;
    if (data && Array.isArray(data.models) && data.models.length) {
      const select = element('select');
      for (const model of data.models) select.append(new Option(model.name, model.id));
      select.value = data.model;
      if (!data.models.some((model) => model.id === data.model)) {
        select.prepend(new Option('Choose an available model', '', true, true));
        select.options[0].disabled = true;
      }
      select.addEventListener('change', () => report(saveModelPreferences(select.value, {})));
      controls.append(preferenceField(prefix, 'model', 'Model', select));
      const model = data.models.find((item) => item.id === data.model);
      if (model) {
        const levels = SIMPLE_THINKING.filter((level) => model.thinking.includes(level));
        if (levels.length) controls.append(settingSelect(prefix, model, 'thinking', levels, 'Thinking'));
        if (prefix === 'settings-model') controls.append(advancedModelSettings(prefix, model, advancedOpen));
      }
    } else if (!modelPicker.busy && !modelPicker.error) {
      controls.append(element('span', 'No models are available. Contact your administrator.'));
    }
    const status = element('p', modelPicker.message, 'model-status');
    status.setAttribute('role', 'status');
    const error = element('p', modelPicker.error, 'error');
    error.setAttribute('role', 'alert');
    root.replaceChildren(controls, status, error);
    root.setAttribute('aria-busy', String(modelPicker.busy));
    if (modelPicker.error) root.append(button('Reload models', 'secondary', loadModelPreferences));
    if (prefix === 'quick-model') {
      const settings = element('a', 'Model settings');
      settings.href = '#/settings';
      root.append(settings);
    }
  }
  const restore = focused && $(focused);
  if (restore && !modelPicker.busy) restore.focus();
}

function advancedModelSettings(prefix, model, open) {
  const details = element('details');
  details.open = open;
  details.append(element('summary', 'Advanced'));
  for (const [field, values] of Object.entries(model.options)) {
    if (!Array.isArray(values) || !values.length) continue;
    if (field === 'thinking' && values.every((value) => SIMPLE_THINKING.includes(value))) continue;
    const label = field === 'thinking' ? 'All thinking levels' : field.replaceAll('_', ' ');
    // Distinct ids for the full thinking selector and the simple selector.
    details.append(settingSelect(`${prefix}-advanced`, model, field, values, label));
  }
  for (const [field, label, min, max, step] of [
    ['max_tokens', 'Maximum output tokens', '1', '', '1'],
    ['temperature', 'Temperature', '0', '2', 'any'],
  ]) {
    const input = element('input');
    Object.assign(input, { type: 'number', min, max, step });
    input.value = modelPicker.data.settings[field] ?? '';
    input.placeholder = model.defaults[field] == null ? 'Default' : `Default (${model.defaults[field]})`;
    input.addEventListener('change', () => {
      if (!input.reportValidity()) return;
      changeModelSetting(field, input.value === '' ? '' : Number(input.value));
    });
    details.append(preferenceField(prefix, field, label, input));
  }
  details.append(element('p', 'Leave a field on Default to use the recommended setting.', 'field-hint'));
  details.append(button('Reset to model defaults', 'secondary', () => saveModelPreferences(model.id, {})));
  return details;
}

async function openSettings() {
  $('settings-title').focus();
  await loadModelPreferences();
}

$('open-settings').addEventListener('click', () => { location.hash = '#/settings'; closeDrawer(); });
renderModelPickers();
