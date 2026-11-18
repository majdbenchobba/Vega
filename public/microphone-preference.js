// Browser-local input choice. Device identifiers never go to the host.
const KEY = 'vega.microphone.v1';
const normalize = value => ({
  deviceId: typeof value?.deviceId === 'string' && value.deviceId.length <= 512 ? value.deviceId : '',
  label: typeof value?.label === 'string' ? value.label.slice(0, 160) : '',
});

export class MicrophonePreference {
  constructor(storage = () => globalThis.window?.localStorage) {
    this.storage = storage;
    this.current = normalize(null);
    this.sessionOnly = false;
  }
  read() {
    if (this.sessionOnly) return { ...this.current };
    try {
      const storage = this.storage();
      if (storage) this.current = normalize(JSON.parse(storage.getItem(KEY) || 'null'));
    } catch { /* Keep the current tab's choice if storage is unavailable. */ }
    return { ...this.current };
  }
  write(value) {
    this.current = normalize(value);
    this.sessionOnly = true;
    try {
      const storage = this.storage();
      if (!storage) return false;
      storage.setItem(KEY, JSON.stringify(this.current));
      this.sessionOnly = false;
      return true;
    } catch { return false; }
  }
}

export const microphonePreference = new MicrophonePreference();
