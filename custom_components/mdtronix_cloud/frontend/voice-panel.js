// The MDtronix Voice page: one switch per device for Google Home and Alexa, styled after Home Assistant's own
// Expose page. Plain JavaScript with no build step and no use of Home Assistant's private components beyond
// its icons, so a Home Assistant update can't break it. Backend: voice_panel.py.

const WS_DEVICES = 'mdtronix_cloud/voice/devices';
const WS_EXPOSE = 'mdtronix_cloud/voice/expose';
const WS_UPDATE_ENTITY = 'config/entity_registry/update';
const OPTIONS_URL = '/config/integrations/integration/mdtronix_cloud';

const PLATFORMS = [
  { key: 'google', name: 'Google Home', column: 'Google' },
  { key: 'alexa', name: 'Alexa', column: 'Alexa' },
];

const FILTERS = [
  { value: 'all', label: 'All devices', test: () => true },
  { value: 'exposed', label: 'Exposed', test: (d) => d.google || d.alexa },
  { value: 'google', label: 'Exposed to Google', test: (d) => d.google },
  { value: 'alexa', label: 'Exposed to Alexa', test: (d) => d.alexa },
  { value: 'none', label: 'Not exposed', test: (d) => !d.google && !d.alexa },
];

const FALLBACK_ICONS = {
  alarm_control_panel: 'mdi:shield-home',
  climate: 'mdi:thermostat',
  cover: 'mdi:window-shutter',
  fan: 'mdi:fan',
  light: 'mdi:lightbulb',
  lock: 'mdi:lock',
  media_player: 'mdi:cast',
  scene: 'mdi:palette',
  script: 'mdi:script-text',
  sensor: 'mdi:eye',
  switch: 'mdi:toggle-switch',
};

const STYLES = `
:host {
  display: block;
  min-height: 100%;
  background: var(--primary-background-color);
  color: var(--primary-text-color);
  font-family: var(--ha-font-family-body, Roboto, Noto, sans-serif);
  font-size: 14px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--header-height, 56px);
  padding: 0 16px;
  box-sizing: border-box;
  background: var(--app-header-background-color, var(--primary-background-color));
  color: var(--app-header-text-color, var(--primary-text-color));
  border-bottom: 1px solid var(--divider-color);
}
.bar h1 { margin: 0; font-size: 20px; font-weight: 400; }
.menu { display: none; background: none; border: 0; padding: 4px; color: inherit; cursor: pointer; }
:host([narrow]) .menu { display: flex; }
.tools { display: flex; gap: 12px; padding: 12px 16px; flex-wrap: wrap; align-items: center; }
.search, select {
  height: 40px;
  box-sizing: border-box;
  padding: 0 12px;
  border: 1px solid var(--divider-color);
  border-radius: 8px;
  background: var(--card-background-color);
  color: var(--primary-text-color);
  font: inherit;
}
.search { flex: 1 1 220px; min-width: 0; }
.summary { color: var(--secondary-text-color); white-space: nowrap; }
.note { margin: 0; padding: 0 16px 12px; color: var(--secondary-text-color); line-height: 1.5; }
.note a, .aliases-empty a { color: var(--primary-color); }
.error { margin: 0 16px 12px; padding: 10px 12px; border-radius: 8px; color: var(--error-color, #db4437); border: 1px solid currentColor; }
.error[hidden] { display: none; }
.status { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); }
table { width: 100%; border-collapse: collapse; }
th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 10px 12px;
  text-align: left;
  font-weight: 500;
  background: var(--primary-background-color);
  border-bottom: 1px solid var(--divider-color);
}
td { padding: 6px 12px; border-bottom: 1px solid var(--divider-color); }
th.toggle, td.toggle { width: 72px; text-align: center; }
td.icon { width: 40px; padding-right: 0; color: var(--state-icon-color, var(--secondary-text-color)); }
td.area, td.aliases { color: var(--secondary-text-color); }
th.icon { width: 40px; padding-right: 0; }
.open {
  display: block;
  width: 100%;
  padding: 8px 0;
  background: none;
  border: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.open:hover { color: var(--primary-color); }
.open small {
  display: none;
  overflow: hidden;
  color: var(--secondary-text-color);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:host([narrow]) .area, :host([narrow]) .aliases { display: none; }
:host([narrow]) .open small { display: block; }
/* A fixed layout, so a long entity ID shortens instead of pushing the Alexa switch off the screen. */
:host([narrow]) table { table-layout: fixed; }
:host([narrow]) th.toggle, :host([narrow]) td.toggle { width: 52px; padding-left: 4px; padding-right: 4px; }
:host([narrow]) th.icon, :host([narrow]) td.icon { width: 24px; }
:host([narrow]) td { overflow-wrap: anywhere; }
.switch {
  appearance: none;
  position: relative;
  display: inline-block;
  width: 36px;
  height: 20px;
  margin: 0;
  border-radius: 10px;
  background: var(--switch-unchecked-track-color, #8a8a8a);
  cursor: pointer;
  vertical-align: middle;
  transition: background 0.15s;
}
.switch::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.15s;
}
.switch:checked { background: var(--primary-color); }
.switch:checked::after { transform: translateX(16px); }
.switch:disabled { opacity: 0.3; cursor: not-allowed; }
.switch:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
dialog {
  width: min(480px, calc(100vw - 32px));
  box-sizing: border-box;
  padding: 24px;
  border: 0;
  border-radius: 16px;
  background: var(--card-background-color);
  color: var(--primary-text-color);
}
dialog::backdrop { background: rgba(0, 0, 0, 0.6); }
.dialog-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.dialog-head h2 { margin: 0; font-size: 20px; font-weight: 500; overflow-wrap: anywhere; }
.close { display: flex; padding: 4px; background: none; border: 0; color: inherit; cursor: pointer; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; font-size: 16px; }
.row small { display: block; margin-top: 2px; color: var(--secondary-text-color); font-size: 12px; }
dialog h3 { margin: 16px 0 4px; font-size: 16px; font-weight: 500; }
.help { margin: 0 0 8px; color: var(--secondary-text-color); }
.alias { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 0; overflow-wrap: anywhere; }
.alias button { display: flex; padding: 4px; background: none; border: 0; color: var(--secondary-text-color); cursor: pointer; }
.add { display: flex; gap: 8px; margin-top: 8px; }
.add input { flex: 1; min-width: 0; }
.add button {
  padding: 0 16px;
  border: 0;
  border-radius: 8px;
  background: var(--primary-color);
  color: var(--text-primary-color, #fff);
  font: inherit;
  cursor: pointer;
}
`;

/** Builds an element. Text only ever goes in as text nodes, so device names can't inject markup. */
function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name.startsWith('on')) element.addEventListener(name.slice(2), value);
    else if (name in element) element[name] = value;
    else element.setAttribute(name, value);
  }
  element.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return element;
}

const namedAliases = (device) => (device.aliases ?? []).filter((alias) => alias !== null);

class MdtronixVoicePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._devices = null;
    this._query = '';
    this._filter = FILTERS[0];
    this._openDevice = null;
  }

  // Home Assistant sets this on every state change, so it must not redraw the page.
  set hass(hass) {
    this._hass = hass;
    if (this._started) return;
    this._started = true;
    this._build();
    this._load();
  }

  set narrow(narrow) {
    this.toggleAttribute('narrow', Boolean(narrow));
  }

  _build() {
    this._search = h('input', {
      class: 'search',
      type: 'search',
      placeholder: 'Search devices',
      'aria-label': 'Search devices',
      oninput: (event) => {
        this._query = event.target.value.trim().toLowerCase();
        this._drawRows();
      },
    });
    const filter = h(
      'select',
      {
        'aria-label': 'Filter devices',
        onchange: (event) => {
          this._filter = FILTERS.find((option) => option.value === event.target.value);
          this._drawRows();
        },
      },
      ...FILTERS.map((option) => h('option', { value: option.value }, option.label)),
    );
    this._summary = h('span', { class: 'summary' });
    this._note = h('p', { class: 'note' });
    this._error = h('p', { class: 'error', role: 'alert', hidden: true });
    this._content = h('div', {}, h('p', { class: 'status' }, 'Loading devices…'));
    this._dialog = h('dialog', { onclose: () => (this._openDevice = null) });

    const menu = h(
      'button',
      {
        class: 'menu',
        'aria-label': 'Open the sidebar',
        onclick: () => this.dispatchEvent(new CustomEvent('hass-toggle-menu', { bubbles: true, composed: true })),
      },
      h('ha-icon', { icon: 'mdi:menu' }),
    );

    this.shadowRoot.append(
      h('style', {}, STYLES),
      h('header', { class: 'bar' }, menu, h('h1', {}, 'MDtronix Voice')),
      h('div', { class: 'tools' }, this._search, filter, this._summary),
      this._note,
      this._error,
      this._content,
      this._dialog,
    );
  }

  async _load() {
    try {
      const result = await this._hass.callWS({ type: WS_DEVICES });
      this._devices = result.devices;
      this._defaultAlias = result.default_alias;
      this._search.placeholder = `Search ${this._devices.length} devices`;
      this._drawNote(result.pin_set);
      this._drawTable();
    } catch (err) {
      this._content.replaceChildren(
        h('p', { class: 'status' }, `Couldn't load your devices: ${err.message ?? err.code ?? err}`),
      );
    }
  }

  _drawNote(pinSet) {
    this._note.replaceChildren(
      'Locks, alarms, doors, garages, gates and windows need a PIN for voice control. ',
      pinSet ? 'A PIN is set. ' : 'No PIN is set yet. ',
      h('a', { href: OPTIONS_URL }, pinSet ? 'Change the PIN' : 'Set the PIN'),
      '. After adding a device for Alexa, say "Alexa, discover devices".',
    );
  }

  _drawTable() {
    this._rows = h('tbody');
    this._content.replaceChildren(
      h(
        'table',
        {},
        h(
          'thead',
          {},
          h(
            'tr',
            {},
            h('th', { class: 'icon' }),
            h('th', {}, 'Name'),
            h('th', { class: 'area' }, 'Area'),
            ...PLATFORMS.map((platform) => h('th', { class: 'toggle' }, platform.column)),
            h('th', { class: 'aliases' }, 'Aliases'),
          ),
        ),
        this._rows,
      ),
    );
    this._drawRows();
  }

  _matches(device) {
    if (!this._filter.test(device)) return false;
    if (!this._query) return true;
    return [device.name, device.area ?? '', device.entity_id, ...namedAliases(device)].some((text) =>
      text.toLowerCase().includes(this._query),
    );
  }

  _drawRows() {
    if (!this._devices) return;
    const shown = this._devices
      .filter((device) => this._matches(device))
      .sort((a, b) => a.name.localeCompare(b.name));
    this._rows.replaceChildren(...shown.map((device) => this._row(device)));
    if (!shown.length) {
      this._rows.append(h('tr', {}, h('td', { colSpan: 6, class: 'status' }, 'No devices match.')));
    }
    this._drawSummary();
  }

  _drawSummary() {
    const count = (key) => this._devices.filter((device) => device[key]).length;
    this._summary.textContent = PLATFORMS.map((platform) => `${platform.column} ${count(platform.key)}`).join(' · ');
  }

  _icon(device) {
    const stateObj = this._hass.states[device.entity_id];
    if (stateObj && customElements.get('ha-state-icon')) {
      return h('ha-state-icon', { hass: this._hass, stateObj });
    }
    return h('ha-icon', { icon: FALLBACK_ICONS[device.entity_id.split('.')[0]] ?? 'mdi:shape' });
  }

  _switch(device, platform) {
    const supported = device[`${platform.key}_supported`];
    return h('input', {
      class: 'switch',
      type: 'checkbox',
      role: 'switch',
      checked: device[platform.key],
      // Something already exposed can always be turned off, even if the device has since gone away.
      disabled: !supported && !device[platform.key],
      title: supported ? '' : `${platform.name} can't control this device`,
      'aria-label': `${device.name}: ${platform.name}`,
      'data-entity': device.entity_id,
      'data-platform': platform.key,
      onchange: (event) => this._setExposed(device, platform, event.target.checked),
    });
  }

  _row(device) {
    const aliases = namedAliases(device).join(', ');
    return h(
      'tr',
      {},
      h('td', { class: 'icon' }, this._icon(device)),
      h(
        'td',
        {},
        h(
          'button',
          { class: 'open', onclick: () => this._openDialog(device) },
          device.name,
          h('small', {}, device.area ?? device.entity_id),
        ),
      ),
      h('td', { class: 'area' }, device.area ?? '—'),
      ...PLATFORMS.map((platform) => h('td', { class: 'toggle' }, this._switch(device, platform))),
      h('td', { class: 'aliases' }, aliases || '—'),
    );
  }

  async _setExposed(device, platform, expose) {
    const previous = device[platform.key];
    device[platform.key] = expose;
    this._syncDevice(device);
    try {
      await this._hass.callWS({ type: WS_EXPOSE, platform: platform.key, entity_ids: [device.entity_id], expose });
      this._showError('');
    } catch (err) {
      device[platform.key] = previous;
      this._syncDevice(device);
      this._showError(`Couldn't update ${device.name}: ${err.message ?? err.code ?? err}`);
    }
  }

  // Only the switches change, not the rows: a device filtered out mid-click shouldn't jump away under the cursor,
  // and redrawing would take keyboard focus off the switch.
  _syncDevice(device) {
    for (const input of this.shadowRoot.querySelectorAll('input.switch')) {
      // By entity, not name: two rooms can each have a device called "Fan".
      if (input.dataset.entity === device.entity_id) input.checked = device[input.dataset.platform];
    }
    this._drawSummary();
  }

  _showError(message) {
    this._error.textContent = message;
    this._error.hidden = !message;
  }

  _openDialog(device) {
    this._openDevice = device;
    this._drawDialog();
    this._dialog.showModal();
  }

  _drawDialog() {
    const device = this._openDevice;
    this._dialog.replaceChildren(
      h(
        'div',
        { class: 'dialog-head' },
        h(
          'button',
          { class: 'close', 'aria-label': 'Close', onclick: () => this._dialog.close() },
          h('ha-icon', { icon: 'mdi:close' }),
        ),
        h('h2', {}, device.name),
      ),
      ...PLATFORMS.map((platform) =>
        h(
          'div',
          { class: 'row' },
          h(
            'div',
            {},
            `Expose to ${platform.name}`,
            device[`${platform.key}_supported`] ? null : h('small', {}, `${platform.name} can't control this device.`),
          ),
          this._switch(device, platform),
        ),
      ),
      h('h3', {}, 'Aliases'),
      device.aliases === null
        ? h('p', { class: 'help' }, "This device has no unique ID, so Home Assistant can't keep aliases for it.")
        : this._aliasEditor(device),
    );
  }

  _aliasEditor(device) {
    const input = h('input', {
      class: 'search',
      placeholder: 'Add alias',
      'aria-label': 'Add alias',
      maxLength: 100,
    });
    const add = (event) => {
      event.preventDefault();
      const alias = input.value.trim();
      if (!alias || namedAliases(device).includes(alias)) return;
      // The first alias is the name Google uses, so an empty list has to keep the default name in front of a new one.
      const current = device.aliases.length || !this._defaultAlias ? device.aliases : [null];
      this._saveAliases(device, [...current, alias]);
    };
    return h(
      'div',
      {},
      h('p', { class: 'help' }, 'Extra names you can use for this device with voice assistants.'),
      ...namedAliases(device).map((alias) =>
        h(
          'div',
          { class: 'alias' },
          alias,
          h(
            'button',
            {
              'aria-label': `Remove alias ${alias}`,
              onclick: () => this._saveAliases(device, device.aliases.filter((item) => item !== alias)),
            },
            h('ha-icon', { icon: 'mdi:close' }),
          ),
        ),
      ),
      h('form', { class: 'add', onsubmit: add }, input, h('button', { type: 'submit' }, 'Add')),
    );
  }

  async _saveAliases(device, aliases) {
    const previous = device.aliases;
    device.aliases = aliases;
    this._drawDialog();
    try {
      await this._hass.callWS({ type: WS_UPDATE_ENTITY, entity_id: device.entity_id, aliases });
      this._showError('');
      this._drawRows();
    } catch (err) {
      device.aliases = previous;
      this._drawDialog();
      this._showError(`Couldn't save the aliases of ${device.name}: ${err.message ?? err.code ?? err}`);
    }
  }
}

customElements.define('mdtronix-voice-panel', MdtronixVoicePanel);
