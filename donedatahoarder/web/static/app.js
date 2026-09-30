/* ============================================================
   DoneDataHoarder — Alpine.js frontend application
   ============================================================ */

document.addEventListener('alpine:init', () => {

  const photoValue = value => {
    const text = String(value == null ? 'Unknown' : typeof value === 'object' ? JSON.stringify(value) : value).replace(/\s+/g, ' ');
    return text.length <= 512 ? text : text.slice(0, 512) + '…';
  };
  const photoDimensions = photo => {
    const width = photo?.display_width || photo?.width;
    const height = photo?.display_height || photo?.height;
    return Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0
      ? `${width}×${height} px · ${(width * height / 1000000).toFixed(1)} MP`
      : 'Pixel dimensions unknown';
  };
  window.photoMetadataText = function (photo) {
    if (photo?.is_photo === false || photo?.status === 'not_photo') return '';
    const fields = photo?.fields || {};
    const lines = [photoDimensions(photo)];
    if (photo?.status !== 'complete') lines.push(`Capture metadata inventory: ${photo?.status || 'unknown'}; absence is not established.`);
    else if (!Object.keys(fields).length) lines.push('No valid capture metadata found.');
    for (const [field, value] of Object.entries(fields)) lines.push(`${field.replaceAll('_', ' ')}: ${photoValue(value)}`);
    for (const warning of photo?.warnings || []) lines.push(photoValue(warning));
    return lines.join('\n');
  };
  window.photoQualityText = function (quality) {
    if (!quality) return '';
    const labels = {
      recommended: 'PHOTO PRESERVATION · Keeper recommendation',
      tradeoff: 'PHOTO TRADEOFF · Keep both for review',
      equivalent: 'PHOTO EVIDENCE · Equivalent recorded evidence',
      unknown: 'PHOTO EVIDENCE UNKNOWN · Keep both for review',
      variant: 'PHOTO VARIANTS · Keep both for review',
    };
    const candidate = quality.candidate || {}, keeper = quality.keeper || {};
    const lines = [labels[quality.status] || labels.unknown,
      `Candidate: ${photoDimensions(candidate)}`, `Keeper: ${photoDimensions(keeper)}`,
      ...(quality.reasons || []).map(photoValue)];
    for (const [label, key, evidence] of [
      ['Only candidate retains', 'candidate_unique_fields', candidate],
      ['Only keeper retains', 'keeper_unique_fields', keeper],
    ]) for (const field of quality[key] || []) lines.push(`${label} ${field.replaceAll('_', ' ')}: ${photoValue(evidence.fields?.[field])}`);
    for (const field of quality.conflicting_fields || []) lines.push(`Conflicting ${field.replaceAll('_', ' ')}: candidate ${photoValue(candidate.fields?.[field])}; keeper ${photoValue(keeper.fields?.[field])}`);
    if (quality.requires_review) lines.push('Inspect both originals. Pixel dimensions alone do not establish image quality or interchangeability.');
    return lines.join('\n');
  };

  window.trapDialogTab = function (event) {
    const dialog = event.currentTarget;
    const choices = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href]')]
      .filter(el => el.getClientRects().length);
    if (!choices.length) { event.preventDefault(); dialog.focus(); return; }
    if (document.activeElement === dialog) {
      event.preventDefault();
      choices[event.shiftKey ? choices.length - 1 : 0].focus();
    } else if (event.shiftKey && document.activeElement === choices[0]) {
      event.preventDefault(); choices[choices.length - 1].focus();
    } else if (!event.shiftKey && document.activeElement === choices[choices.length - 1]) {
      event.preventDefault(); choices[0].focus();
    }
  };

  /* ----------------------------------------------------------
   * Global store
   * -------------------------------------------------------- */
  Alpine.store('app', {
    tab: 'home',
    toasts: [],
    loading: false,
    foregroundOperation: null,
    foregroundOperationSeq: 0,
    reviewQueue: null,
    version: '0.6.0',

    toast(msg, type = 'info') {
      const id = Date.now();
      this.toasts.push({ id, msg, type });
      setTimeout(() => this.dismissToast(id), type === 'error' ? 6000 : 3000);
    },
    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },
  });

  Alpine.store('dedupCoverage', {
    sessionId: null,
    jobState: null,
    stages: {},
    async load() {
      const sid = Alpine.store('session').current_session_id;
      if (!sid) { this.sessionId = null; this.jobState = null; this.stages = {}; return; }
      try {
        const result = await api.get(`/pipeline/dedup/coverage?session_id=${encodeURIComponent(sid)}`);
        if (sid !== Alpine.store('session').current_session_id) return;
        this.sessionId = sid;
        this.jobState = result.job_state;
        this.stages = result.stages || {};
      } catch (_) { /* Preserve the last known coverage until the service reconnects. */ }
    },
    get incompleteStages() {
      return ['perceptual', 'semantic', 'text_near']
        .filter(name => this.stages[name]?.candidate_coverage === 'bounded_incomplete');
    },
    get deferredLabel() {
      const values = this.incompleteStages.map(name => this.stages[name]?.candidate_pair_opportunities_deferred);
      return values.some(value => value == null)
        ? 'some candidate pairs were deferred; exact count unknown'
        : `${values.reduce((sum, value) => sum + value, 0).toLocaleString()} candidate pair opportunities deferred`;
    },
  });

  /* ----------------------------------------------------------
   * Modal store — in-app confirm / prompt.
   * Native dialogs never surface inside the embedded window.
   * -------------------------------------------------------- */
  Alpine.store('modal', {
    open: false,
    mode: 'confirm',
    title: '',
    message: '',
    value: '',
    confirmLabel: 'OK',
    cancelLabel: 'Cancel',
    danger: false,
    _resolve: null,
    _returnFocus: null,

    _activateFocus() {
      document.querySelector('.app-shell')?.setAttribute('inert', '');
      requestAnimationFrame(() => {
        const dialog = document.querySelector('.app-dialog .modal');
        if (!dialog) return;
        const initial = this.mode === 'prompt'
          ? dialog.querySelector('input')
          : dialog.querySelector('button.btn-outline');
        (initial || dialog).focus();
      });
    },

    _releaseFocus() {
      document.querySelector('.app-shell')?.removeAttribute('inert');
      const target = this._returnFocus;
      this._returnFocus = null;
      requestAnimationFrame(() => target?.isConnected && target.focus());
    },

    trapTab(event) {
      if (!this.open) return;
      const dialog = document.querySelector('.app-dialog .modal');
      if (!dialog) return;
      const choices = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled])')]
        .filter(el => el.getClientRects().length);
      if (!choices.length) { event.preventDefault(); dialog.focus(); return; }
      const first = choices[0], last = choices[choices.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    },

    ask(opts = {}) {
      if (this._resolve) {
        const prev = this._resolve;
        this._resolve = null;
        prev(this.mode === 'prompt' ? null : false);
      }
      this.mode = opts.mode === 'prompt' ? 'prompt' : 'confirm';
      this.title = opts.title || '';
      this.message = opts.message || '';
      this.value = opts.value != null ? String(opts.value) : '';
      this.confirmLabel = opts.confirmLabel || 'OK';
      this.cancelLabel = opts.cancelLabel || 'Cancel';
      this.danger = !!opts.danger;
      this._returnFocus = document.activeElement;
      this.open = true;
      this._activateFocus();
      return new Promise((resolve) => {
        this._resolve = resolve;
      });
    },

    accept() {
      if (!this.open || !this._resolve) return;
      const resolve = this._resolve;
      const result = this.mode === 'prompt' ? this.value : true;
      this._resolve = null;
      this.open = false;
      this._releaseFocus();
      resolve(result);
    },

    cancel() {
      if (!this.open || !this._resolve) return;
      const resolve = this._resolve;
      const result = this.mode === 'prompt' ? null : false;
      this._resolve = null;
      this.open = false;
      this._releaseFocus();
      resolve(result);
    },

    // Escape always cancels. Enter confirms a prompt (input or not).
    onEscape(event) {
      if (!this.open) return;
      if (event) event.preventDefault();
      this.cancel();
    },

    onEnter(event) {
      if (!this.open || this.mode !== 'prompt') return;
      if (event) event.preventDefault();
      this.accept();
    },
  });

  window.appConfirm = function (message, opts = {}) {
    return Alpine.store('modal').ask({
      mode: 'confirm',
      message: message == null ? '' : String(message),
      title: opts.title || '',
      confirmLabel: opts.confirmLabel || 'OK',
      cancelLabel: opts.cancelLabel || 'Cancel',
      danger: !!opts.danger,
    });
  };

  window.appPrompt = function (message, defaultValue = '', opts = {}) {
    return Alpine.store('modal').ask({
      mode: 'prompt',
      message: message == null ? '' : String(message),
      value: defaultValue == null ? '' : String(defaultValue),
      title: opts.title || '',
      confirmLabel: opts.confirmLabel || 'OK',
      cancelLabel: opts.cancelLabel || 'Cancel',
      danger: !!opts.danger,
    });
  };

  /* ----------------------------------------------------------
   * Session store — tracks the current active session
   * -------------------------------------------------------- */
  Alpine.store('session', {
    current_session_id: null,
    name: null,
    is_unsaved: false,
    created_at: null,
    updated_at: null,
    last_saved_at: null,
    root_path: '',
    backend: 'ollama',
    model: '',
    analyze_model: '',
    propose_model: '',
    skip_dirs: [],
    workers: 1,
    preferred_language: 'leave_as_is',
    relate_scope: 'per_directory',
    stats: {},
    file_count: 0,
    proposal_count: 0,
    duplicate_count: 0,

    get active() {
      return !!this.current_session_id;
    },

    get displayName() {
      return this.name || 'Unnamed Session';
    },

    get hasScanned() {
      const steps = this.stats?.completed_steps || [];
      return steps.includes('scan');
    },

    clear() {
      this.current_session_id = null;
      this.name = null;
      this.is_unsaved = false;
      this.created_at = null;
      this.updated_at = null;
      this.last_saved_at = null;
      this.root_path = '';
      this.preferred_language = 'leave_as_is';
      this.stats = {};
      this.file_count = 0;
      this.proposal_count = 0;
      this.duplicate_count = 0;
    },

    loadFrom(data) {
      this.current_session_id = data.id;
      this.name = data.name;
      this.is_unsaved = data.is_unsaved || false;
      this.created_at = data.created_at;
      this.updated_at = data.updated_at;
      this.last_saved_at = data.last_saved_at;
      this.root_path = data.root_path || '';
      this.backend = data.backend || 'ollama';
      this.model = data.model || '';
      this.analyze_model = data.analyze_model || '';
      this.propose_model = data.propose_model || '';
      this.workers = data.workers || 1;
      this.preferred_language = data.preferred_language || 'leave_as_is';
      this.relate_scope = data.relate_scope || 'per_directory';
      this.stats = data.stats || {};
      this.file_count = data.file_count || 0;
      this.proposal_count = data.proposal_count || 0;
      this.duplicate_count = data.duplicate_count || 0;
      window.dispatchEvent(new CustomEvent('datahoarder:session-loaded'));
    },
  });

  /* ----------------------------------------------------------
   * API helper
   * -------------------------------------------------------- */
  window.api = {
    async get(url) {
      const res = await fetch(`/api${url}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    },
    async post(url, body = {}) {
      const res = await fetch(`/api${url}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const error = await res.json().catch(() => ({}));
        const failure = new Error(error.detail || `${res.status} ${res.statusText}`);
        failure.status = res.status;
        throw failure;
      }
      return res.json();
    },
    async patch(url, body = {}) {
      const res = await fetch(`/api${url}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    },
    async del(url) {
      const res = await fetch(`/api${url}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    },
  };

  /* ----------------------------------------------------------
   * Initialize app version from backend
   * -------------------------------------------------------- */
  (async () => {
    try {
      const info = await api.get('/info');
      Alpine.store('app').version = info.version;
    } catch (e) {
      // Fallback to default version if API call fails
      console.warn('Failed to load version:', e.message);
    }
  })();

  /* ----------------------------------------------------------
   * Refresh helper — reloads all data tabs after pipeline ops
   * -------------------------------------------------------- */
  // Track a global "data version" — bumped after every pipeline action.
  // Tabs check this on activation to know if they need to reload.
  window._dataVersion = 0;

  window.refreshAllTabs = async function () {
    window._dataVersion++;
    document.dispatchEvent(new CustomEvent('datahoarder:refresh'));
  };

  window.saveCurrentSession = async function () {
    const session = Alpine.store('session');
    if (!session.active) return;

    let name = session.name;
    if (!name) {
      name = await window.appPrompt(
        'Enter a name for this session:',
        `Session ${new Date().toLocaleDateString()}`,
        { title: 'Save session', confirmLabel: 'Save' }
      );
      if (!name) return;
    }

    try {
      const data = await api.post(`/sessions/${session.current_session_id}/save`, { name });
      session.name = data.name;
      session.is_unsaved = false;
      session.last_saved_at = data.last_saved_at;
      Alpine.store('app').toast(`Session saved: ${data.name}`, 'success');
    } catch (e) {
      Alpine.store('app').toast('Failed to save session: ' + e.message, 'error');
    }
  };

  window.goHome = async function () {
    const session = Alpine.store('session');
    if (session.active && session.is_unsaved) {
      const leave = await window.appConfirm(
        'You have unsaved changes. Leave without saving?',
        { title: 'Unsaved changes', confirmLabel: 'Leave', danger: true }
      );
      if (!leave) return;
    }
    session.clear();
    Alpine.store('app').tab = 'home';
  };

  /* ----------------------------------------------------------
   * Home component — session list, create/load/delete sessions
   * -------------------------------------------------------- */
  Alpine.data('home', () => ({
    sessions: [],
    loading: false,

    async init() {
      await this.loadSessions();
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'home') this.loadSessions();
      });
    },

    async loadSessions() {
      this.loading = true;
      try {
        const data = await api.get('/sessions');
        this.sessions = data.items || [];
      } catch (e) {
        Alpine.store('app').toast('Failed to load sessions: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    async createSession() {
      try {
        // Create new session with empty model to force user selection
        const data = await api.post('/sessions', {
          root_path: '',
          backend: 'ollama',
          model: '',
          workers: 1,
          preferred_language: localStorage.getItem('datahoarder_preferred_language') || 'leave_as_is',
          relate_scope: localStorage.getItem('datahoarder_relate_scope') || 'per_directory',
        });
        Alpine.store('session').loadFrom(data);
        // Clear localStorage so new session doesn't inherit old settings
        localStorage.removeItem('datahoarder_model');
        localStorage.removeItem('datahoarder_analyze_model');
        localStorage.removeItem('datahoarder_propose_model');
        localStorage.removeItem('datahoarder_folder');
        localStorage.removeItem('datahoarder_workers');
        // Signal setup component to reset its fields
        window.dispatchEvent(new CustomEvent('datahoarder:new-session'));
        Alpine.store('app').tab = 'setup';
        Alpine.store('app').toast('New session created. Configure your settings and run the pipeline.', 'success');
      } catch (e) {
        Alpine.store('app').toast('Failed to create session: ' + e.message, 'error');
      }
    },

    async loadSession(sessionId) {
      try {
        const data = await api.get(`/sessions/${sessionId}`);
        Alpine.store('session').loadFrom(data);
        // Restore settings to localStorage for compatibility
        if (data.root_path) localStorage.setItem('datahoarder_folder', data.root_path);
        if (data.model) localStorage.setItem('datahoarder_model', data.model);
        if (data.analyze_model) localStorage.setItem('datahoarder_analyze_model', data.analyze_model);
        if (data.propose_model) localStorage.setItem('datahoarder_propose_model', data.propose_model);
        if (data.backend) localStorage.setItem('datahoarder_backend', data.backend);
        if (data.workers) localStorage.setItem('datahoarder_workers', String(data.workers));
        if (data.preferred_language) localStorage.setItem('datahoarder_preferred_language', data.preferred_language);
        if (data.relate_scope) localStorage.setItem('datahoarder_relate_scope', data.relate_scope);
        Alpine.store('app').tab = 'dashboard';
        Alpine.store('app').toast(`Loaded session: ${data.name || 'Unnamed Session'}`, 'success');
        // Refresh all data tabs
        await refreshAllTabs();
      } catch (e) {
        Alpine.store('app').toast('Failed to load session: ' + e.message, 'error');
      }
    },

    async deleteSession(sessionId, sessionName) {
      const name = sessionName || 'Unnamed Session';
      const ok = await window.appConfirm(
        `Permanently delete "${name}"? This cannot be undone.`,
        { title: 'Delete session', confirmLabel: 'Delete', danger: true }
      );
      if (!ok) return;
      try {
        await api.del(`/sessions/${sessionId}`);
        this.sessions = this.sessions.filter(s => s.id !== sessionId);
        // If we deleted the active session, clear it
        if (Alpine.store('session').current_session_id === sessionId) {
          Alpine.store('session').clear();
        }
        Alpine.store('app').toast(`Deleted session: ${name}`, 'success');
      } catch (e) {
        Alpine.store('app').toast('Failed to delete session: ' + e.message, 'error');
      }
    },

    formatDate(isoStr) {
      if (!isoStr) return '-';
      const d = new Date(isoStr);
      const now = new Date();
      const diffMs = now - d;
      const mins = Math.floor(diffMs / 60000);
      if (mins < 1) return 'just now';
      if (mins < 60) return `${mins} min ago`;
      const hours = Math.floor(mins / 60);
      if (hours < 24) return `${hours}h ago`;
      const days = Math.floor(hours / 24);
      if (days < 7) return `${days}d ago`;
      return d.toLocaleDateString();
    },

    stepIcon(steps, step) {
      return (steps || []).includes(step) ? '✓' : '○';
    },

    stepClass(steps, step) {
      return (steps || []).includes(step) ? 'step-done' : 'step-pending';
    },
  }));

  /* ----------------------------------------------------------
   * Unsaved changes warning
   * -------------------------------------------------------- */
  window.addEventListener('beforeunload', (e) => {
    const session = Alpine.store('session');
    if (session && session.active && session.is_unsaved) {
      e.preventDefault();
      e.returnValue = 'You have unsaved changes. Are you sure you want to leave?';
    }
  });

  /* ----------------------------------------------------------
   * Results mixin — shared save/load functionality
   * -------------------------------------------------------- */
  const resultsMixin = {
    currentResult: '',
    savedResults: [],
    snapshotMode: false,
    snapshotItems: [],
    snapshotSavedTotal: 0,
    snapshotAsOf: '',
    _resultsListRequest: 0,
    _resultLoadRequest: 0,

    resultType() {
      if (this.files !== undefined) return 'files';
      if (this.proposals !== undefined) return 'proposals';
      return 'duplicates';
    },

    resetResults() {
      this._resultsListRequest += 1;
      this._resultLoadRequest += 1;
      this.currentResult = '';
      this.savedResults = [];
      this.clearSnapshot();
      this.loadResultsList();
    },

    clearSnapshot() {
      this.snapshotMode = false;
      this.snapshotItems = [];
      this.snapshotSavedTotal = 0;
      this.snapshotAsOf = '';
      this.currentResult = '';
    },

    showSnapshotPage() {
      if (!this.snapshotMode) return;
      const rows = this.snapshotItems.slice((this.page - 1) * this.perPage, this.page * this.perPage);
      const key = this.resultType() === 'files' ? 'files' : this.resultType() === 'proposals' ? 'proposals' : 'groups';
      this[key] = rows;
      this.total = this.snapshotItems.length;
    },

    async initResults() {
      await this.loadResultsList();
    },

    async loadResultsList() {
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this._resultsListRequest;
      if (!sid) { this.savedResults = []; return; }
      try {
        const rows = await api.get(`/results/list?session_id=${encodeURIComponent(sid)}&result_type=${this.resultType()}`);
        if (requestId === this._resultsListRequest && sid === Alpine.store('session').current_session_id)
          this.savedResults = rows;
      } catch (e) {
        console.error('Failed to load results list:', e);
      }
    },

    async saveResults(type) {
      const sid = Alpine.store('session').current_session_id;
      if (!sid || type !== this.resultType()) return;
      const name = await window.appPrompt(
        `Save this session's ${type === 'proposals' ? 'pending proposals' : type} snapshot as:`,
        `${type}_${new Date().toISOString().slice(0, 10)}`,
        { title: 'Save results', confirmLabel: 'Save' }
      );
      if (!name || sid !== Alpine.store('session').current_session_id) return;

      try {
        const result = await api.post(`/results/save/${type}?session_id=${encodeURIComponent(sid)}&name=${encodeURIComponent(name)}`);
        if (sid !== Alpine.store('session').current_session_id) return;
        Alpine.store('app').toast(`Saved ${result.filename} (${result.message})`, 'success');
        await this.loadResultsList();
        this.currentResult = '';
      } catch (e) {
        Alpine.store('app').toast(`Failed to save results: ${e.message}`, 'error');
      }
    },

    async loadResult() {
      if (!this.currentResult) return;
      const sid = Alpine.store('session').current_session_id;
      const filename = this.currentResult;
      const type = this.resultType();
      const requestId = ++this._resultLoadRequest;
      if (!sid) return;

      try {
        const result = await api.get(`/results/load/${encodeURIComponent(filename)}?session_id=${encodeURIComponent(sid)}&result_type=${type}`);
        if (requestId !== this._resultLoadRequest || sid !== Alpine.store('session').current_session_id ||
            filename !== this.currentResult) return;
        if (result.session_id !== sid || result.type !== type || !Array.isArray(result.data?.items))
          throw new Error('Saved result does not belong to this session and view');
        const data = result.data;
        this._listRequest += 1;
        this._loadedFilters = null;
        this._loadedVersion = -1;
        if (this.cancelDuplicateReview) this.cancelDuplicateReview();
        if (this.selectedFile !== undefined) { this.selectedFile = null; this.showModal = false; }
        this.snapshotMode = true;
        this.snapshotItems = data.items;
        this.snapshotSavedTotal = data.total;
        this.snapshotAsOf = result.saved_at || '';
        this.page = 1;
        this.showSnapshotPage();
        Alpine.store('app').toast(`Loaded historical ${type} snapshot (read-only)`, 'info');
      } catch (e) {
        if (requestId === this._resultLoadRequest && sid === Alpine.store('session').current_session_id)
          Alpine.store('app').toast(`Failed to load results: ${e.message}`, 'error');
      }
    },
  };

  /* ----------------------------------------------------------
   * Dashboard component
   * -------------------------------------------------------- */
  Alpine.data('dashboard', () => ({
    stats: null,
    activeJob: null,
    latestPlan: null,
    jobStatusError: false,
    planStatusError: false,
    statusLoading: false,
    statusSessionId: null,
    _loadedVersion: -1,
    _statsRequest: 0,
    _statusRequest: 0,
    _statusInFlightSession: null,
    _statusInFlightPromise: null,
    _statusPoll: null,
    async init() {
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'dashboard') {
          if (this._loadedVersion < window._dataVersion) this.load();
          else this.loadExecutionStatus();
        }
      });
      this.$watch(() => Alpine.store('session').current_session_id, () => this.load());
      this._statusPoll = setInterval(() => {
        if (Alpine.store('app').tab === 'dashboard') this.loadExecutionStatus();
      }, 5000);
    },
    destroy() {
      if (this._statusPoll) clearInterval(this._statusPoll);
    },
    async load() {
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this._statsRequest;
      const dataVersion = window._dataVersion;
      this.stats = null;
      this._loadedVersion = -1;
      const statusRequest = this.loadExecutionStatus();
      try {
        const url = sid ? `/stats?session_id=${encodeURIComponent(sid)}` : '/stats';
        const stats = await api.get(url);
        if (requestId !== this._statsRequest || sid !== Alpine.store('session').current_session_id) return;
        this.stats = stats;
        this._loadedVersion = dataVersion;
      } catch (e) {
        if (requestId === this._statsRequest && sid === Alpine.store('session').current_session_id)
          Alpine.store('app').toast('Failed to load stats', 'error');
      }
      await statusRequest;
    },
    async loadExecutionStatus() {
      const sid = Alpine.store('session').current_session_id;
      if (this._statusInFlightPromise && this._statusInFlightSession === sid)
        return this._statusInFlightPromise;
      const requestId = ++this._statusRequest;
      if (sid !== this.statusSessionId) {
        this.statusSessionId = sid;
        this.activeJob = null;
        this.latestPlan = null;
        this.jobStatusError = false;
        this.planStatusError = false;
        this.statusLoading = !!sid;
      }
      if (!sid) { this.statusLoading = false; return; }
      const request = Promise.allSettled([
        api.get('/pipeline/jobs/active'),
        api.get(`/pipeline/runs/latest?session_id=${encodeURIComponent(sid)}`),
      ]).then(([jobResult, planResult]) => {
        if (requestId !== this._statusRequest || sid !== Alpine.store('session').current_session_id) return;
        this.jobStatusError = jobResult.status !== 'fulfilled';
        this.planStatusError = planResult.status !== 'fulfilled';
        this.activeJob = !this.jobStatusError && jobResult.value.job_id && jobResult.value.session_id === sid
          ? jobResult.value : null;
        this.latestPlan = planResult.status === 'fulfilled' ? planResult.value.plan : null;
        this.statusLoading = false;
      });
      this._statusInFlightSession = sid;
      this._statusInFlightPromise = request;
      try { await request; } finally {
        if (this._statusInFlightPromise === request) {
          this._statusInFlightPromise = null;
          this._statusInFlightSession = null;
        }
      }
    },
    executionState() {
      if (Alpine.store('app').foregroundOperation?.session_id === this.statusSessionId)
        return 'Running';
      if (this.statusLoading) return 'Checking';
      if (this.activeJob) {
        const state = this.activeJob.state;
        if (state === 'paused') return 'Paused';
        if (state === 'cancelling') return 'Cancelling';
        if (state === 'running') return 'Running';
      }
      if (this.jobStatusError || this.planStatusError) return 'Unavailable';
      const planState = this.latestPlan?.state;
      if (planState === 'paused') return 'Paused';
      if (planState === 'cancelling') return 'Cancelling';
      if (planState === 'failed') return 'Failed';
      if (planState === 'interrupted') return 'Interrupted';
      if (planState === 'running') return 'Running';
      if (!this.statusSessionId) return 'No session';
      return 'Idle';
    },
    executionSummary() {
      const state = this.executionState();
      if (state === 'Running') return this.activeJob
        ? `Processing ${this.activeJob.job_type || 'files'}`
        : Alpine.store('app').foregroundOperation?.session_id === this.statusSessionId
          ? `Processing ${Alpine.store('app').foregroundOperation.step}`
          : 'Managed run is between steps';
      if (state === 'Paused') return 'Processing paused';
      if (state === 'Cancelling') return 'Stopping processing';
      if (state === 'Failed') return 'Latest managed run failed';
      if (state === 'Interrupted') return 'Latest managed run needs recovery';
      if (state === 'Unavailable') return 'Processing status unavailable';
      if (state === 'Checking') return 'Checking processing status';
      if (state === 'No session') return 'Choose a session to see its processing status';
      return 'No active app processing';
    },
    executionDetail() {
      const state = this.executionState();
      if (state === 'Idle' && this.latestPlan?.state === 'completed')
        return 'The latest managed run completed. Review proposals before applying changes.';
      if (state === 'Idle' && this.latestPlan?.state === 'ready')
        return 'The latest managed run is ready to continue.';
      if (state === 'Idle' && this.latestPlan?.state === 'cancelled')
        return 'The latest managed run was cancelled.';
      if (state === 'Failed' || state === 'Interrupted')
        return 'Open Pipeline to inspect the saved run and recovery options.';
      if (state === 'Idle') return 'No app job or action from this browser is running for this session.';
      if (state === 'Unavailable') return 'Could not check the job service; this is not a completion signal.';
      return '';
    },
    reviewWaiting() {
      const pending = this.stats?.proposal_counts?.pending || 0;
      return this.executionState() === 'Idle' && pending > 0
        ? `${pending.toLocaleString()} suggestions awaiting review` : '';
    },
    pendingReviewTypes() {
      const counts = this.stats?.pending_by_type || {};
      return [
        ['rename', 'Renames'], ['rename_folder', 'Folder renames'],
        ['move', 'Moves'], ['add_tags', 'Tags'], ['update_metadata', 'Metadata'],
        ['mark_duplicate', 'Duplicates'],
      ].map(([type, label]) => ({ type, label, count: counts[type] || 0 }))
        .filter(item => item.count > 0);
    },
    openReviewQueue(type = '') {
      const sid = Alpine.store('session').current_session_id;
      if (!sid || !this.stats?.proposal_counts?.pending) return;
      Alpine.store('app').reviewQueue = { session_id: sid, type };
      Alpine.store('app').tab = 'proposals';
    },
    formatBytes(b) {
      if (!b) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      const i = Math.floor(Math.log(b) / Math.log(1024));
      return (b / Math.pow(1024, i)).toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
    },
    fileStatusDistribution() {
      if (!this.stats) return [];
      const s = this.stats.by_status || {};
      const total = this.stats.total_files || 1;
      return [
        { label: 'Pending',  count: s.pending  || 0 },
        { label: 'Enriched', count: s.enriched || 0 },
        { label: 'Analyzed', count: s.analyzed || 0 },
        { label: 'Proposed', count: s.proposed || 0 },
        { label: 'Applied',  count: s.applied  || 0 },
        { label: 'Skipped',  count: s.skipped  || 0 },
        { label: 'Error',    count: s.error    || 0 },
      ].map(item => ({ ...item, share: `${(item.count / total * 100).toFixed(1)}%` }))
        .filter(item => item.count > 0);
    },
  }));

  /* ----------------------------------------------------------
   * Files browser (with results mixin)
   * -------------------------------------------------------- */
  Alpine.data('fileBrowser', () => ({
    files: [],
    total: 0,
    page: 1,
    perPage: 50,
    search: '',
    statusFilter: '',
    mimeFilter: '',
    selectedFile: null,
    showModal: false,
    _loadedVersion: -1,
    _listRequest: 0,
    _detailRequest: 0,
    _sessionId: null,
    _loadedFilters: null,
    ...resultsMixin,

    async init() {
      await this.initResults();
      await Alpine.store('dedupCoverage').load();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'files' && this._loadedVersion < window._dataVersion) this.load();
      });
      this.$watch(() => Alpine.store('session').current_session_id, () => this.onSessionChange());
      if (this._sessionId !== Alpine.store('session').current_session_id) this.onSessionChange();
    },

    onSessionChange() {
      this.resetResults();
      this._listRequest += 1;
      this._detailRequest += 1;
      this._sessionId = null;
      this._loadedFilters = null;
      this._loadedVersion = -1;
      this.files = []; this.total = 0; this.page = 1;
      this.search = ''; this.statusFilter = ''; this.mimeFilter = '';
      this.showModal = false; this.selectedFile = null;
      if (Alpine.store('app').tab === 'files') this.load();
    },

    async load() {
      this._resultLoadRequest += 1;
      this.clearSnapshot();
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this._listRequest;
      const filters = JSON.stringify([this.page, this.perPage, this.statusFilter, this.mimeFilter, this.search]);
      this._sessionId = sid || null;
      this._loadedFilters = null;
      this.files = []; this.total = 0;
      if (!sid) return;
      try {
        let url = `/files?page=${this.page}&per_page=${this.perPage}`;
        if (this.statusFilter) url += `&status=${this.statusFilter}`;
        if (this.mimeFilter) url += `&mime_prefix=${this.mimeFilter}`;
        if (this.search) url += `&search=${encodeURIComponent(this.search)}`;
        url += `&session_id=${encodeURIComponent(sid)}`;
        const data = await api.get(url);
        if (requestId !== this._listRequest || sid !== Alpine.store('session').current_session_id ||
            filters !== JSON.stringify([this.page, this.perPage, this.statusFilter, this.mimeFilter, this.search])) return;
        this.files = data.items;
        this.total = data.total;
        this._loadedFilters = filters;
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        if (requestId === this._listRequest && sid === Alpine.store('session').current_session_id)
          Alpine.store('app').toast('Failed to load files', 'error');
      }
    },

    totalPages() { return Math.ceil(this.total / this.perPage) || 1; },

    async viewFile(id) {
      const sid = Alpine.store('session').current_session_id;
      const row = this.files.find(file => file.id === id);
      if (!sid || this._sessionId !== sid || !row) return;
      if (this._loadedFilters !== JSON.stringify([this.page, this.perPage, this.statusFilter, this.mimeFilter, this.search])) return;
      const requestId = ++this._detailRequest;
      const listRequest = this._listRequest;
      const filters = this._loadedFilters;
      try {
        this._fileDialogReturnFocus = document.activeElement;
        const detail = await api.get(`/files/${id}`);
        if (requestId !== this._detailRequest || sid !== Alpine.store('session').current_session_id ||
            listRequest !== this._listRequest || filters !== this._loadedFilters ||
            this._sessionId !== sid || !this.files.some(file => file.id === id && file.path === row.path) ||
            detail.id !== id || detail.path !== row.path) return;
        this.selectedFile = detail;
        this.showModal = true;
      } catch (e) {
        if (requestId === this._detailRequest && sid === Alpine.store('session').current_session_id)
          Alpine.store('app').toast('Failed to load file details', 'error');
      }
    },

    closeModal() {
      this._detailRequest += 1;
      this.showModal = false; this.selectedFile = null;
      this.$nextTick(() => this._fileDialogReturnFocus?.isConnected && this._fileDialogReturnFocus.focus());
    },

    isImage(f) {
      return f.mime_type && f.mime_type.startsWith('image/');
    },

    formatSize(b) {
      if (!b) return '-';
      if (b > 1024*1024) return (b/1024/1024).toFixed(1) + ' MB';
      return (b/1024).toFixed(0) + ' KB';
    },

    searchDebounced: null,
    onSearch() {
      clearTimeout(this.searchDebounced);
      this.searchDebounced = setTimeout(() => { this.page = 1; this.load(); }, 350);
    },

    async prevPage() { if (this.page > 1) { this.page--; if (this.snapshotMode) this.showSnapshotPage(); else await this.load(); } },
    async nextPage() { if (this.page < this.totalPages()) { this.page++; if (this.snapshotMode) this.showSnapshotPage(); else await this.load(); } },
  }));

  /* ----------------------------------------------------------
   * Proposals review (with results mixin)
   * -------------------------------------------------------- */
  Alpine.data('proposalReview', () => ({
    proposals: [],
    total: 0,
    page: 1,
    perPage: 50,
    statusFilter: 'pending',
    typeFilter: '',
    search: '',
    minConfidence: 0,
    bulkConfidence: 80,
    duplicateReview: null,
    duplicateReviewFiles: null,
    duplicateReviewSessionId: null,
    duplicateReviewReturnFocus: null,
    duplicateReviewRequestId: 0,
    _loadedVersion: -1,
    _listRequest: 0,
    _sessionId: null,
    _loadedFilters: null,
    ...resultsMixin,

    async init() {
      await this.initResults();
      await Alpine.store('dedupCoverage').load();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'proposals') {
          if (!this.applyReviewQueue() && this._loadedVersion < window._dataVersion) this.load();
        }
      });
      this.$watch(() => Alpine.store('session').current_session_id, () => this.onSessionChange());
      if (this._sessionId !== Alpine.store('session').current_session_id) this.onSessionChange();
      else if (Alpine.store('app').tab === 'proposals') this.applyReviewQueue();
    },

    onSessionChange() {
      this.resetResults();
      this._listRequest += 1;
      this.cancelDuplicateReview();
      this._sessionId = null; this._loadedVersion = -1;
      this._loadedFilters = null;
      this.proposals = []; this.total = 0; this.page = 1;
      this.statusFilter = 'pending'; this.typeFilter = ''; this.search = ''; this.minConfidence = 0;
      this.editingId = null; this.editValue = '';
      if (Alpine.store('app').reviewQueue?.session_id !== Alpine.store('session').current_session_id)
        Alpine.store('app').reviewQueue = null;
      if (Alpine.store('app').tab === 'proposals') {
        if (!this.applyReviewQueue()) this.load();
      }
    },

    applyReviewQueue() {
      const queue = Alpine.store('app').reviewQueue;
      if (!queue) return false;
      Alpine.store('app').reviewQueue = null;
      if (queue.session_id !== Alpine.store('session').current_session_id) return false;
      this.page = 1; this.statusFilter = 'pending'; this.typeFilter = queue.type;
      this.search = ''; this.minConfidence = 0;
      this.load();
      return true;
    },

    async load() {
      this._resultLoadRequest += 1;
      this.clearSnapshot();
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this._listRequest;
      const filters = JSON.stringify([this.page, this.perPage, this.statusFilter, this.typeFilter,
        this.search, this.minConfidence]);
      this._sessionId = sid || null;
      this._loadedFilters = null;
      this.proposals = []; this.total = 0;
      this.cancelDuplicateReview();
      this.editingId = null;
      if (!sid) return;
      try {
        let url = `/proposals?page=${this.page}&per_page=${this.perPage}`;
        if (this.statusFilter) url += `&status=${this.statusFilter}`;
        if (this.typeFilter)   url += `&proposal_type=${this.typeFilter}`;
        if (this.minConfidence > 0) url += `&min_confidence=${this.minConfidence / 100}`;
        if (this.search) url += `&search=${encodeURIComponent(this.search)}`;
        url += `&session_id=${encodeURIComponent(sid)}`;
        const data = await api.get(url);
        if (requestId !== this._listRequest || sid !== Alpine.store('session').current_session_id ||
            filters !== JSON.stringify([this.page, this.perPage, this.statusFilter, this.typeFilter,
              this.search, this.minConfidence])) return;
        this.proposals = data.items;
        this.total = data.total;
        this._loadedFilters = filters;
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        if (requestId === this._listRequest && sid === Alpine.store('session').current_session_id)
          Alpine.store('app').toast('Failed to load proposals', 'error');
      }
    },

    async requestApproval(p) {
      if (!this.canActOn(p.id)) return;
      if (p.proposal_type !== 'mark_duplicate' || p.duplicate_evidence?.type === 'exact') {
        return this.approve(p.id);
      }
      if (!p.duplicate_evidence?.keeper_id || !p.duplicate_evidence?.keeper_path) {
        Alpine.store('app').toast('Keeper comparison is unavailable', 'error');
        return;
      }
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this.duplicateReviewRequestId;
      const returnFocus = document.activeElement;
      try {
        const [candidate, keeper] = await Promise.all([
          api.get(`/files/${p.file_id}`),
          api.get(`/files/${p.duplicate_evidence.keeper_id}`),
        ]);
        if (!sid || this._sessionId !== sid || requestId !== this.duplicateReviewRequestId ||
            sid !== Alpine.store('session').current_session_id ||
            candidate.id !== p.file_id || candidate.path !== p.file_path ||
            keeper.id !== p.duplicate_evidence.keeper_id ||
            keeper.path !== p.duplicate_evidence.keeper_path ||
            !this.proposals.some(item => item.id === p.id &&
              (item.status === 'pending' || item.status === 'modified'))) {
          throw new Error('Comparison changed');
        }
        this.duplicateReviewSessionId = sid;
        this.duplicateReviewReturnFocus = returnFocus;
        this.duplicateReviewFiles = { candidate, keeper };
        this.duplicateReview = p;
        this.$nextTick(() => {
          if (this.duplicateReview !== p) return;
          document.querySelector('.app-shell')?.setAttribute('inert', '');
          const dialog = document.querySelector('.duplicate-review-dialog');
          dialog?.focus({ preventScroll: true });
          if (dialog) dialog.scrollTop = 0;
        });
      } catch (_) {
        if (sid === Alpine.store('session').current_session_id && requestId === this.duplicateReviewRequestId)
          Alpine.store('app').toast('Could not load the current candidate and keeper; review again', 'error');
      }
    },

    cancelDuplicateReview() {
      this.duplicateReviewRequestId += 1;
      if (!this.duplicateReview && !this.duplicateReviewFiles) return;
      const returnFocus = this.duplicateReviewReturnFocus;
      this.duplicateReview = null;
      this.duplicateReviewFiles = null;
      this.duplicateReviewSessionId = null;
      this.duplicateReviewReturnFocus = null;
      document.querySelector('.app-shell')?.removeAttribute('inert');
      this.$nextTick(() => returnFocus?.isConnected && returnFocus.focus());
    },

    async approveReviewedDuplicate() {
      const candidate = this.duplicateReview;
      const files = this.duplicateReviewFiles;
      const sid = Alpine.store('session').current_session_id;
      if (!candidate || !files || !sid || sid !== this.duplicateReviewSessionId ||
          !this.canActOn(candidate.id) ||
          !this.proposals.some(p => p.id === candidate.id &&
            (p.status === 'pending' || p.status === 'modified'))) {
        this.cancelDuplicateReview();
        Alpine.store('app').toast('Comparison changed; load proposals again', 'error');
        return;
      }
      const comparison = {
        expected_duplicate_group_id: candidate.duplicate_evidence.group_id,
        expected_duplicate_type: candidate.duplicate_evidence.type,
        expected_keeper_id: files.keeper.id,
        expected_candidate_path: files.candidate.path,
        expected_keeper_path: files.keeper.path,
        ...(candidate.review_token ? { expected_review_token: candidate.review_token } : {}),
      };
      this.cancelDuplicateReview();
      await this.approve(candidate.id, comparison);
    },

    canActOn(id) {
      const sid = Alpine.store('session').current_session_id;
      return !!sid && !this.snapshotMode && this._sessionId === sid &&
        this._loadedFilters === JSON.stringify([this.page, this.perPage, this.statusFilter,
          this.typeFilter, this.search, this.minConfidence]) && this.proposals.some(p => p.id === id &&
        (p.status === 'pending' || p.status === 'modified'));
    },

    markReviewChanged() {
      window._dataVersion += 1;
    },

    async approve(id, comparison = null) {
      if (!this.canActOn(id)) return;
      const sid = Alpine.store('session').current_session_id;
      const proposal = this.proposals.find(p => p.id === id);
      try {
        await api.post(`/proposals/${id}/approve`, {
          session_id: sid,
          ...(proposal?.review_token ? { expected_review_token: proposal.review_token } : {}),
          ...(comparison || {}),
        });
        if (sid !== Alpine.store('session').current_session_id || this._sessionId !== sid) return;
        this.proposals = this.proposals.map(p => p.id === id ? { ...p, status: 'approved' } : p);
        this.markReviewChanged();
        Alpine.store('app').toast('Approved', 'success');
      } catch (e) {
        if (sid !== Alpine.store('session').current_session_id || this._sessionId !== sid) return;
        Alpine.store('app').toast(`Approve failed: ${e.message}`, 'error');
        if (e.status === 409) {
          this.markReviewChanged();
          await this.load();
        }
      }
    },

    async reject(id) {
      if (!this.canActOn(id)) return;
      const sid = Alpine.store('session').current_session_id;
      try {
        await api.post(`/proposals/${id}/reject`, { session_id: sid });
        if (sid !== Alpine.store('session').current_session_id || this._sessionId !== sid) return;
        this.proposals = this.proposals.map(p => p.id === id ? { ...p, status: 'rejected' } : p);
        this.markReviewChanged();
        Alpine.store('app').toast('Rejected', 'success');
      } catch (e) {
        if (sid === Alpine.store('session').current_session_id) Alpine.store('app').toast('Reject failed', 'error');
      }
    },

    editingId: null,
    editValue: '',

    startEdit(p) {
      if (!this.canActOn(p.id)) return;
      if (p.proposal_type === 'mark_duplicate') {
        Alpine.store('app').toast('Choose the keeper in Duplicate Review', 'info');
        return;
      }
      this.editingId = p.id;
      this.editValue = p.proposed_value || '';
    },

    async saveEdit(id) {
      if (!this.canActOn(id) || this.editingId !== id) return;
      const sid = Alpine.store('session').current_session_id;
      const value = this.editValue;
      try {
        const updated = await api.post(`/proposals/${id}/edit`, { session_id: sid, proposed_value: value });
        if (sid !== Alpine.store('session').current_session_id || this._sessionId !== sid) return;
        this.proposals = this.proposals.map(p =>
          p.id === id ? { ...p, proposed_value: updated.proposed_value,
            proposed_path: updated.proposed_value, review_token: updated.review_token, status: 'modified' } : p
        );
        this.editingId = null;
        this.markReviewChanged();
        Alpine.store('app').toast('Updated', 'success');
      } catch (e) {
        if (sid === Alpine.store('session').current_session_id) Alpine.store('app').toast('Edit failed', 'error');
      }
    },

    cancelEdit() { this.editingId = null; },

    async bulkApprove() {
      const sessionId = Alpine.store('session').current_session_id;
      try {
        if (!sessionId || this._sessionId !== sessionId || this.snapshotMode) throw new Error('Load this session first');
        const threshold = this.bulkConfidence / 100;
        const type = this.typeFilter || '';
        const filters = JSON.stringify([this.page, this.perPage, this.statusFilter, this.typeFilter,
          this.search, this.minConfidence]);
        const stillCurrent = () => sessionId === Alpine.store('session').current_session_id &&
          this._sessionId === sessionId && this._loadedFilters === filters &&
          filters === JSON.stringify([this.page, this.perPage, this.statusFilter, this.typeFilter,
            this.search, this.minConfidence]) && threshold === this.bulkConfidence / 100;
        if (!stillCurrent() || this.statusFilter !== 'pending' || this.search) {
          Alpine.store('app').toast('Load pending review and clear search before approving across the session', 'info');
          return;
        }
        const match = await api.get(`/proposals?session_id=${encodeURIComponent(sessionId)}&status=pending&min_confidence=${threshold}&proposal_type=${encodeURIComponent(type)}&per_page=1`);
        if (!stillCurrent()) return;
        if (!match.total) {
          Alpine.store('app').toast('No pending proposals match this threshold', 'info');
          return;
        }
        const scope = type ? `${type.replaceAll('_', ' ')} proposal(s)` : 'proposals across all types';
        if (!await window.appConfirm(`Review bulk approval for up to ${match.total} pending ${scope} in this session with a model-reported score of at least ${threshold * 100}%. Protected resources, similarity-only duplicates, and renames without verified content will be skipped.`, { title: 'Approve matching proposals', confirmLabel: 'Approve' })) return;
        if (!stillCurrent()) return;
        const data = await api.post('/proposals/bulk-approve', {
          session_id: sessionId,
          min_confidence: threshold,
          proposal_type: type || null,
        });
        if (sessionId !== Alpine.store('session').current_session_id || this._sessionId !== sessionId) return;
        this.markReviewChanged();
        Alpine.store('app').toast(
          `Approved ${data.approved}; skipped ${data.skipped_protected || 0} protected, ${data.skipped_near_duplicate || 0} near-duplicates, ${data.skipped_unverified_rename || 0} unverified renames`,
          data.approved ? 'success' : 'info'
        );
        await this.load();
      } catch (e) {
        if (sessionId === Alpine.store('session').current_session_id)
          Alpine.store('app').toast('Bulk approve failed', 'error');
      }
    },

    totalPages() { return Math.ceil(this.total / this.perPage) || 1; },
    async prevPage() { if (this.page > 1) { this.page--; if (this.snapshotMode) this.showSnapshotPage(); else await this.load(); } },
    async nextPage() { if (this.page < this.totalPages()) { this.page++; if (this.snapshotMode) this.showSnapshotPage(); else await this.load(); } },

    confColor(c) {
      if (!c) return 'var(--text-dim)';
      if (c >= 0.8) return 'var(--success)';
      if (c >= 0.5) return 'var(--warning)';
      return 'var(--danger)';
    },
    hasImage(fileId, mime) {
      return !!fileId && !!mime && mime.startsWith('image/');
    },
    comparisonProvenance(file) {
      if (!file) return 'Analysis provenance unavailable';
      const outcome = file.analysis_outcome;
      const source = file.analysis_evidence_source;
      if (outcome === 'content_verified') {
        const medium = source === 'vision' ? 'image' : source === 'text' ? 'text' : 'file content';
        return `AI read ${medium}; its interpretation has not been independently verified.`;
      }
      if (outcome === 'context_only') {
        if (source === 'text' && file.analysis_content_chars > 0)
          return `AI received ${file.analysis_content_chars} extracted text characters; evidence was too limited to verify the content subject.`;
        if (source === 'filename_only') return 'Filename and folder context only; file content was not read by AI.';
        return 'Limited context evidence; content subject remains unverified.';
      }
      if (outcome === 'metadata_only') return 'Metadata or file structure only; content subject was not established.';
      if (outcome === 'skipped' || outcome === 'failed') return 'No content analysis available.';
      return 'Analysis provenance unknown; inspect the original file.';
    },
    duplicateEvidenceText(evidence) {
      return [this.duplicateByteEvidenceText(evidence), window.photoQualityText(evidence?.photo_quality)].filter(Boolean).join('\n');
    },
    duplicateByteEvidenceText(evidence) {
      if (!evidence) return 'Keeper evidence is unavailable; review this candidate individually.';
      if (evidence.exact_bytes === true) return 'Stored SHA-256 hashes match the keeper; execution rechecks the live bytes.';
      if (evidence.exact_bytes === false) return 'Stored SHA-256 hashes differ from the keeper. Do not discard this candidate.';
      if (evidence.type === 'exact') {
        return evidence.matching_indexed_md5 === true
          ? 'Indexed MD5 hashes match; stored SHA-256 is unavailable. Execution checks live SHA-256 before trash.'
          : 'Indexed MD5 match is unavailable; execution checks live SHA-256 before trash.';
      }
      if (evidence.type === 'perceptual' && evidence.distance_to_keeper != null) {
        return `Direct pHash distance to keeper: ${evidence.distance_to_keeper}/${evidence.perceptual_bits || '?'} bits. Similar appearance is not proof of a disposable copy.`;
      }
      if (evidence.similarity_score != null) {
        return `Direct ${evidence.type} similarity to keeper: ${evidence.similarity_score.toFixed(2)}. This is not a safety probability.`;
      }
      return 'Direct keeper comparison is unavailable; inspect both files before deciding.';
    },
  }));

  /* ----------------------------------------------------------
   * Duplicates (with results mixin)
   * -------------------------------------------------------- */
  Alpine.data('duplicates', () => ({
    groups: [],
    total: 0,
    page: 1,
    _loadedVersion: -1,
    _listRequest: 0,
    _sessionId: null,
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'duplicates' && this._loadedVersion < window._dataVersion) this.load();
      });
      this.$watch(() => Alpine.store('session').current_session_id, () => this.onSessionChange());
      if (this._sessionId !== Alpine.store('session').current_session_id) this.onSessionChange();
    },

    onSessionChange() {
      this.resetResults();
      this._listRequest += 1;
      this._sessionId = null; this._loadedVersion = -1;
      this.groups = []; this.total = 0; this.page = 1;
      if (Alpine.store('app').tab === 'duplicates') this.load();
    },

    async load() {
      this._resultLoadRequest += 1;
      this.clearSnapshot();
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this._listRequest;
      this._sessionId = sid || null;
      this.groups = []; this.total = 0;
      if (!sid) return;
      try {
        const data = await api.get(`/duplicates?page=${this.page}&per_page=20&session_id=${encodeURIComponent(sid)}`);
        if (requestId !== this._listRequest || sid !== Alpine.store('session').current_session_id) return;
        this.groups = data.items;
        this.total = data.total;
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        if (requestId === this._listRequest && sid === Alpine.store('session').current_session_id)
          Alpine.store('app').toast('Failed to load duplicates', 'error');
      }
    },

    async setKeeper(groupId, fileId) {
      const sid = Alpine.store('session').current_session_id;
      const displayedGroup = this.groups.find(group => group.id === groupId);
      if (!sid || this.snapshotMode || this._sessionId !== sid ||
          !displayedGroup?.files.some(file => file.id === fileId)) return;
      try {
        const result = await api.post(`/duplicates/${groupId}/keeper`, {
          session_id: sid, keep_file_id: fileId, expected_keeper_id: displayedGroup.keep_file_id,
        });
        if (sid !== Alpine.store('session').current_session_id) return;
        await this.load();
        window._dataVersion++;
        Alpine.store('app').toast(
          result.review_reset ? `Keeper changed; ${result.review_reset} candidate decisions need fresh review` : 'Keeper set',
          'success'
        );
      } catch (e) {
        Alpine.store('app').toast(`Failed to set keeper: ${e.message}`, 'error');
      }
    },

    formatBytes(b) {
      if (!b) return '0 B';
      if (b > 1024*1024*1024) return (b/1024/1024/1024).toFixed(1) + ' GB';
      if (b > 1024*1024) return (b/1024/1024).toFixed(1) + ' MB';
      return (b/1024).toFixed(0) + ' KB';
    },

    perPage: 20,
    totalPages() { return Math.ceil(this.total / this.perPage) || 1; },
    async prevPage() { if (this.page > 1) { this.page--; if (this.snapshotMode) this.showSnapshotPage(); else await this.load(); } },
    async nextPage() { if (this.page < this.totalPages()) { this.page++; if (this.snapshotMode) this.showSnapshotPage(); else await this.load(); } },

    isImage(f) { return f.mime_type && f.mime_type.startsWith('image/'); },
    keeper(g) { return g.files.find(f => f.id === g.keep_file_id) || null; },
    candidates(g) { return g.files.filter(f => f.id !== g.keep_file_id); },
    evidenceText(g, f) {
      return [this.byteEvidenceText(g, f), window.photoQualityText(f.photo_quality)].filter(Boolean).join('\n');
    },
    byteEvidenceText(g, f) {
      if (f.exact_bytes_to_keeper === true) return 'Stored SHA-256 hashes match; live bytes are checked again before trash.';
      if (f.exact_bytes_to_keeper === false) return 'Stored SHA-256 hashes differ; do not discard this candidate.';
      if (g.dupe_type === 'exact') {
        return f.matching_indexed_md5 === true
          ? 'Indexed MD5 hashes match; stored SHA-256 is unavailable. Live SHA-256 is checked before trash.'
          : 'Indexed MD5 match is unavailable; live SHA-256 is checked before trash.';
      }
      if (g.dupe_type === 'perceptual' && f.distance_to_keeper != null) {
        return `Direct pHash distance: ${f.distance_to_keeper}/${f.perceptual_bits || '?'} bits from keeper. Inspect visual differences.`;
      }
      if (f.similarity_score != null) return `Direct ${g.dupe_type} similarity: ${f.similarity_score.toFixed(2)}; not a disposal probability.`;
      return 'No direct measured comparison to the selected keeper.';
    },
  }));

  /* ----------------------------------------------------------
   * Setup component — folder, model, backend, workers
   * -------------------------------------------------------- */
  Alpine.data('setup', () => ({
    selectedFolder: localStorage.getItem('datahoarder_folder') || '',
    selectedAnalyzeModel: localStorage.getItem('datahoarder_analyze_model') || localStorage.getItem('datahoarder_model') || '',
    selectedProposeModel: localStorage.getItem('datahoarder_propose_model') || localStorage.getItem('datahoarder_model') || '',
    selectedBackend: localStorage.getItem('datahoarder_backend') || 'ollama',
    selectedWorkers: parseInt(localStorage.getItem('datahoarder_workers') || '1', 10),
    numParallel: parseInt(localStorage.getItem('datahoarder_num_parallel') || '1', 10),
    preferredLanguage: localStorage.getItem('datahoarder_preferred_language') || 'leave_as_is',
    relateScope: localStorage.getItem('datahoarder_relate_scope') || 'per_directory',
    customModel: '',
    showCustomModel: false,
    showBrowser: false,
    currentPath: '',
    parentPath: null,
    drives: [],
    folders: [],
    ollamaStatus: null,
    installedModels: [],
    recommendedModels: [],
    pulling: null,
    pullProgress: {},
    subfolders: [],
    skippedFolders: [],
    dbPath: '',

    syncFromLoadedSession() {
      const session = Alpine.store('session');
      if (!session.current_session_id) return;
      this.selectedFolder = session.root_path || '';
      this.selectedAnalyzeModel = session.analyze_model || session.model || '';
      this.selectedProposeModel = session.propose_model || session.model || '';
      this.selectedBackend = session.backend || 'ollama';
      this.selectedWorkers = Number(session.workers) || 1;
      this.preferredLanguage = session.preferred_language || 'leave_as_is';
      this.relateScope = session.relate_scope || 'per_directory';
    },

    async init() {
      window.addEventListener('datahoarder:session-loaded', () => this.syncFromLoadedSession());
      this.syncFromLoadedSession();
      await this.loadOllamaStatus();
      await this.loadInstalledModels();
      await this.loadDbPath();
      this.syncFromLoadedSession();
      // Reset folder/model fields when a new session is created
      window.addEventListener('datahoarder:new-session', () => {
        this.selectedFolder = '';
        this.selectedAnalyzeModel = '';
        this.selectedProposeModel = '';
        this.selectedBackend = 'ollama';
        this.selectedWorkers = 1;
        this.subfolders = [];
        this.skippedFolders = [];
      });
      this.$watch('showBrowser', (val) => {
        if (val) {
          this._browserReturnFocus = document.activeElement;
          if (!this.currentPath) this.browsePath('');
        } else {
          this.$nextTick(() => this._browserReturnFocus?.isConnected && this._browserReturnFocus.focus());
        }
      });
      this.recommendedModels = [
        { name: 'gemma4:31b',  desc: 'Gemma 4 31B - Highest quality, dense, multimodal, 256K context', size: '20 GB', vision: true, latest: true },
        { name: 'gemma4:26b',  desc: 'Gemma 4 26B - Mixture of Experts, balanced, multimodal, 256K context', size: '18 GB', vision: true, latest: true },
        { name: 'gemma4:e4b',  desc: 'Gemma 4 E4B - Edge variant, multimodal+audio, 128K context', size: '9.6 GB', vision: true, latest: true },
        { name: 'gemma4:e2b',  desc: 'Gemma 4 E2B - Lightweight edge, multimodal+audio, 128K context', size: '7.2 GB', vision: true, latest: true },
        { name: 'gemma2:27b',  desc: 'Gemma 2 27B - High quality, multimodal, needs 20GB+ RAM', size: '16 GB', vision: true },
        { name: 'gemma2:9b',   desc: 'Gemma 2 9B - Best balance of quality/speed', size: '5.5 GB', vision: true },
        { name: 'gemma3:12b',  desc: 'Gemma 3 12B - Good quality, multimodal', size: '8.1 GB', vision: true },
        { name: 'gemma3:4b',   desc: 'Gemma 3 4B - Fast, lightweight, multimodal', size: '3.3 GB', vision: true },
        { name: 'llava:13b',   desc: 'LLaVA 13B - Specialized vision model', size: '8.0 GB', vision: true },
        { name: 'llava:7b',    desc: 'LLaVA 7B - Lightweight vision', size: '4.7 GB', vision: true },
        { name: 'llama3.2:3b', desc: 'Llama 3.2 3B - Fast text-only, 2GB', size: '2.0 GB', vision: false },
      ];
    },

    async browsePath(path) {
      try {
        const data = await api.get(`/browse?path=${encodeURIComponent(path)}`);
        this.currentPath = data.current;
        this.parentPath = data.parent;
        this.drives = data.drives;
        this.folders = data.folders;
      } catch (e) {
        Alpine.store('app').toast('Failed to browse: ' + e.message, 'error');
      }
    },

    goBack() {
      if (this.parentPath) {
        this.browsePath(this.parentPath);
      } else {
        this.currentPath = '';
        this.parentPath = null;
        this.drives = [];
        this.folders = [];
      }
    },

    selectFolder(path) {
      this.selectedFolder = path;
      this.showBrowser = false;
      this.saveSettings();
      this.loadSubfolders();
      Alpine.store('app').toast('Folder selected: ' + path, 'success');
    },

    async loadSubfolders() {
      if (!this.selectedFolder) { this.subfolders = []; return; }
      try {
        const data = await api.get(`/subfolders?root_path=${encodeURIComponent(this.selectedFolder)}`);
        this.subfolders = data.folders || [];
        this.skippedFolders = this.subfolders.filter(f => f.completed).map(f => f.path);
      } catch (e) { this.subfolders = []; }
    },

    toggleSkipFolder(path) {
      const idx = this.skippedFolders.indexOf(path);
      if (idx >= 0) this.skippedFolders.splice(idx, 1);
      else this.skippedFolders.push(path);
    },

    async loadDbPath() {
      try {
        const data = await api.get('/db-info');
        this.dbPath = data.db_path || '';
      } catch (e) { /* ignore */ }
    },

    async saveDbPath() {
      try {
        await api.post('/db-info', { db_path: this.dbPath });
        Alpine.store('app').toast('DB path saved. Restart the server to apply.', 'success');
      } catch (e) {
        Alpine.store('app').toast('Failed to save DB path: ' + e.message, 'error');
      }
    },

    selectCustomModel(target) {
      if (!this.customModel.trim()) {
        Alpine.store('app').toast('Enter a model name', 'error');
        return;
      }
      const name = this.customModel.trim();
      if (target === 'propose') this.selectedProposeModel = name;
      else this.selectedAnalyzeModel = name;
      this.customModel = '';
      this.showCustomModel = false;
      this.saveSettings();
      Alpine.store('app').toast('Custom model selected: ' + name, 'success');
    },

    saveSettings() {
      localStorage.setItem('datahoarder_folder', this.selectedFolder);
      localStorage.setItem('datahoarder_analyze_model', this.selectedAnalyzeModel);
      localStorage.setItem('datahoarder_propose_model', this.selectedProposeModel);
      localStorage.setItem('datahoarder_backend', this.selectedBackend);
      localStorage.setItem('datahoarder_workers', String(this.selectedWorkers));
      localStorage.setItem('datahoarder_num_parallel', String(this.numParallel));
      localStorage.setItem('datahoarder_preferred_language', this.preferredLanguage);
      localStorage.setItem('datahoarder_relate_scope', this.relateScope);
      // Sync to session store and persist to backend
      const session = Alpine.store('session');
      if (session.active) {
        session.root_path = this.selectedFolder;
        session.model = this.selectedAnalyzeModel;
        session.analyze_model = this.selectedAnalyzeModel;
        session.propose_model = this.selectedProposeModel;
        session.backend = this.selectedBackend;
        session.workers = this.selectedWorkers;
        session.preferred_language = this.preferredLanguage;
        session.relate_scope = this.relateScope;
        // Persist to DB
        api.patch(`/sessions/${session.current_session_id}`, {
          root_path: this.selectedFolder,
          backend: this.selectedBackend,
          model: this.selectedAnalyzeModel,
          analyze_model: this.selectedAnalyzeModel,
          propose_model: this.selectedProposeModel,
          workers: this.selectedWorkers,
          preferred_language: this.preferredLanguage,
          relate_scope: this.relateScope,
        }).catch(() => {}); // fire-and-forget
      }
    },

    async loadOllamaStatus() {
      try {
        this.ollamaStatus = await api.get('/ollama/status');
      } catch (e) {
        Alpine.store('app').toast('Failed to check Ollama status', 'error');
      }
    },

    async loadInstalledModels() {
      try {
        const data = await api.get('/ollama/models');
        this.installedModels = data.models;
      } catch (e) {
        Alpine.store('app').toast('Failed to load models', 'error');
      }
    },

    isInstalled(modelName) {
      return this.installedModels.some(m =>
        m.name === modelName ||
        m.name === modelName + ':latest' ||
        m.name.split(':')[0] === modelName.split(':')[0] && m.name.split(':')[1] === modelName.split(':')[1]
      );
    },

    async pullModel(modelName) {
      this.pulling = modelName;
      this.pullProgress[modelName] = 0;
      let maxProgress = 0;
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 3600000);

        const response = await fetch(`/api/ollama/pull`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: modelName }),
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        if (!response.ok) {
          throw new Error(`${response.status} ${response.statusText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();

          if (value) {
            buffer += decoder.decode(value, { stream: true });
          }

          if (done) {
            buffer += decoder.decode();
            break;
          }

          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.progress !== undefined) {
                  maxProgress = Math.max(maxProgress, data.progress);
                  this.pullProgress[modelName] = maxProgress;
                }
                if (data.status === 'error') {
                  Alpine.store('app').toast(`Pull failed: ${data.message}`, 'error');
                  this.pulling = null;
                  return;
                }
              } catch (e) {
                // Ignore JSON parse errors
              }
            }
          }
        }

        if (buffer) {
          const lines = buffer.split('\n');
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.progress !== undefined) {
                  maxProgress = Math.max(maxProgress, data.progress);
                  this.pullProgress[modelName] = maxProgress;
                }
              } catch (e) {}
            }
          }
        }

        let verified = false;
        for (let attempt = 0; attempt < 3; attempt++) {
          await new Promise(r => setTimeout(r, 2000));
          await this.loadInstalledModels();
          if (this.isInstalled(modelName)) {
            verified = true;
            break;
          }
        }

        if (verified) {
          Alpine.store('app').toast(`Downloaded ${modelName}`, 'success');
        } else {
          Alpine.store('app').toast(`Download completed but model not found in Ollama. Try running "ollama pull ${modelName}" from command line.`, 'warning');
        }
      } catch (e) {
        Alpine.store('app').toast(`Pull failed: ${e.message}`, 'error');
      } finally {
        this.pulling = null;
        delete this.pullProgress[modelName];
      }
    },

    async deleteModel(modelName) {
      const ok = await window.appConfirm(
        `Are you sure you want to delete ${modelName}? This cannot be undone.`,
        { title: 'Delete model', confirmLabel: 'Delete', danger: true }
      );
      if (!ok) return;
      try {
        await api.post(`/ollama/delete`, { model: modelName });
        Alpine.store('app').toast(`Deleted ${modelName}`, 'success');
        await this.loadInstalledModels();
      } catch (e) {
        Alpine.store('app').toast(`Delete failed: ${e.message}`, 'error');
      }
    },

    async startOllama() {
      try {
        const res = await api.post('/ollama/start', { num_parallel: this.numParallel });
        Alpine.store('app').toast(res.status, 'success');
        await new Promise(r => setTimeout(r, 3000));
        await this.loadOllamaStatus();
      } catch (e) {
        Alpine.store('app').toast('Failed to start Ollama: ' + e.message, 'error');
      }
    },

    async restartOllama() {
      Alpine.store('app').toast('Restarting Ollama with NUM_PARALLEL=' + this.numParallel + '...', 'info');
      try {
        const res = await api.post('/ollama/restart', { num_parallel: this.numParallel });
        Alpine.store('app').toast('Ollama ' + res.status + (res.num_parallel ? ' (parallel=' + res.num_parallel + ')' : ''), 'success');
        await new Promise(r => setTimeout(r, 2000));
        await this.loadOllamaStatus();
      } catch (e) {
        Alpine.store('app').toast('Failed to restart Ollama: ' + e.message, 'error');
      }
    },
  }));

  /* ----------------------------------------------------------
   * Pipeline runner — reads settings from localStorage (Setup tab)
   * -------------------------------------------------------- */
  Alpine.data('pipeline', () => ({
    running: null,
    result: null,
    commitPreview: null,
    preflight: null,
    preflightBusy: false,
    preflightError: null,
    analysisCoverage: null,
    organizationCoverage: null,
    sequenceSampleStride: 0,
    useAnalysisCache: true,
    // Progress tracking for background jobs (one per type)
    analyzeProgress: null,
    enrichProgress: null,
    dedupProgress: null,
    relateProgress: null,
    proposeProgress: null,
    organizeProgress: null,
    executeProgress: null,
    progressStartTime: null,
    // Background job state
    activeJobId: null,
    activeJobType: null,
    activeJobSessionId: null,
    _activeJobRequest: 0,
    _runStepRequestId: 0,
    jobState: null,  // 'running', 'paused', 'completed', 'failed', 'cancelled'
    _eventSource: null,
    // Unattended mode state
    unattendedMode: false,
    unattendedQueue: [],
    unattendedCurrentStep: null,
    unattendedStartTime: null,
    unattendedCompletedSteps: [],
    unattendedFailedStep: null,
    runPlanId: null,
    runPlanSessionId: null,
    runPlanState: null,
    runPlanError: null,
    analysisErrors: null,
    _runPlanPoll: null,
    _runPlanRequest: 0,
    // Wake Lock state — prevents the system from sleeping during unattended runs
    _wakeLock: null,
    _wakeLockSupported: typeof navigator !== 'undefined' && 'wakeLock' in navigator,
    _onVisibilityChange: null,
    wakeLockActive: false,

    async init() {
      // Check for an active background job (reconnect after page refresh)
      await Alpine.store('dedupCoverage').load();
      await this.checkActiveJob();
      await this.checkRunPlan();
      await this.loadAnalysisErrors();
      await this.loadOrganizationCoverage();
      this.$watch(() => Alpine.store('session').current_session_id, () => {
        this.checkRunPlan();
        this.checkActiveJob();
      });
      this._runPlanPoll = setInterval(() => this.checkRunPlan(), 2500);
    },

    async loadPreflight() {
      const sid = Alpine.store('session').current_session_id;
      if (!sid) {
        this.preflightError = 'Choose a session folder in Setup first.';
        return;
      }
      this.preflightBusy = true;
      this.preflightError = null;
      this.preflight = null;
      try {
        const stride = Number(this.sequenceSampleStride) || 0;
        const mode = stride ? 'representative' : 'full';
        const skips = (Alpine.store('session').skip_dirs || [])
          .map(dir => `&skip_dirs=${encodeURIComponent(dir)}`).join('');
        this.preflight = await api.get(`/pipeline/preflight?session_id=${encodeURIComponent(sid)}&mode=${mode}&sequence_sample_stride=${stride}${skips}`);
      } catch (e) {
        this.preflightError = e.message;
      } finally {
        this.preflightBusy = false;
      }
    },

    async loadOrganizationCoverage() {
      const sid = Alpine.store('session').current_session_id;
      if (!sid) { this.organizationCoverage = null; return; }
      try {
        const value = await api.get(`/pipeline/organize/coverage?session_id=${encodeURIComponent(sid)}`);
        if (sid === Alpine.store('session').current_session_id) this.organizationCoverage = value;
      } catch (_) { this.organizationCoverage = null; }
    },

    async loadAnalysisCoverage() {
      const sid = Alpine.store('session').current_session_id;
      if (!sid) { this.analysisCoverage = null; return; }
      try {
        const value = await api.get(`/pipeline/analysis/coverage?session_id=${encodeURIComponent(sid)}`);
        if (sid === Alpine.store('session').current_session_id) this.analysisCoverage = value;
      } catch (_) {
        if (sid === Alpine.store('session').current_session_id) this.analysisCoverage = null;
      }
    },

    async checkRunPlan() {
      const sid = Alpine.store('session').current_session_id;
      this.clearOtherSessionJob(sid);
      const requestId = ++this._runPlanRequest;
      if (sid !== this.runPlanSessionId) {
        this.runPlanSessionId = sid;
        this.runPlanId = null;
        this.runPlanState = null;
        this.runPlanError = null;
        this.unattendedMode = false;
        this.unattendedQueue = [];
        this.unattendedCurrentStep = null;
        this.unattendedCompletedSteps = [];
        this.unattendedFailedStep = null;
        this.analysisErrors = null;
        this.analysisCoverage = null;
        if (sid) await this.loadAnalysisCoverage();
      }
      if (!sid) return;
      try {
        const response = await api.get(`/pipeline/runs/latest?session_id=${encodeURIComponent(sid)}`);
        if (requestId !== this._runPlanRequest || sid !== Alpine.store('session').current_session_id || sid !== this.runPlanSessionId) return;
        const plan = response.plan;
        if (!plan) {
          this.runPlanId = null;
          this.runPlanState = null;
          this.runPlanError = null;
          this.unattendedMode = false;
          this.unattendedQueue = [];
          this.unattendedCurrentStep = null;
          this.unattendedCompletedSteps = [];
          this.unattendedFailedStep = null;
          return;
        }
        const previous = this.runPlanState;
        if (this.runPlanId !== plan.plan_id) {
          this.sequenceSampleStride = Number(plan.options?.sequence_sample_stride) || 0;
          this.useAnalysisCache = plan.options?.use_cache !== false;
        }
        this.runPlanId = plan.plan_id;
        this.runPlanState = plan.state;
        this.runPlanError = response.last_failure?.error || null;
        this.unattendedMode = ['running', 'paused', 'cancelling'].includes(plan.state);
        this.unattendedCurrentStep = plan.steps[plan.current_index] || null;
        this.unattendedCompletedSteps = plan.completed_steps || [];
        this.unattendedQueue = plan.steps.slice(plan.current_index + (this.unattendedMode ? 1 : 0));
        this.unattendedFailedStep = ['failed', 'interrupted', 'cancelled'].includes(plan.state)
          ? this.unattendedCurrentStep : null;
        this.unattendedStartTime = plan.created_at ? Date.parse(plan.created_at) : null;
        if (plan.active_job_id && plan.active_job_id !== this.activeJobId) {
          await this.checkActiveJob();
        }
        if (previous && previous !== plan.state && plan.state === 'completed') {
          Alpine.store('app').toast('Unattended run complete. Review proposals before applying changes.', 'success');
        }
        if (['failed', 'interrupted'].includes(plan.state) && previous !== plan.state) {
          await this.loadAnalysisErrors();
        }
      } catch (e) {
        this.runPlanError = e.message;
      }
    },

    async loadAnalysisErrors() {
      const sid = Alpine.store('session').current_session_id;
      if (!sid) return;
      try {
        const errors = await api.get(`/pipeline/analyze/errors?session_id=${encodeURIComponent(sid)}`);
        if (sid === Alpine.store('session').current_session_id) this.analysisErrors = errors;
      } catch (_) { /* the run status remains visible */ }
    },

    async retryAnalysisErrors() {
      const settings = this.getSettings();
      if (!settings.session_id) return;
      try {
        const response = await api.post('/pipeline/analyze', {
          session_id: settings.session_id, backend: settings.backend,
          model: settings.analyzeModel, workers: settings.workers, retry_errors: true,
        });
        if (settings.session_id !== Alpine.store('session').current_session_id) return;
        this.activeJobId = response.job_id;
        this.activeJobType = 'analyze';
        this.activeJobSessionId = settings.session_id;
        this.jobState = 'running';
        this.running = 'analyze';
        this._connectJobStream(response.job_id, 'analyze');
      } catch (e) {
        Alpine.store('app').toast(`Retry failed: ${e.message}`, 'error');
      }
    },

    async resumeUnattended() {
      if (!this.runPlanId) return;
      const sid = Alpine.store('session').current_session_id;
      try {
        const action = this.runPlanState === 'ready' ? 'advance' : 'resume';
        const response = await api.post(`/pipeline/runs/${this.runPlanId}/${action}`, {
          session_id: sid, retry_errors: !!this.analysisErrors?.retryable,
        });
        if (sid !== Alpine.store('session').current_session_id) return;
        this.runPlanState = response.plan.state;
        await this.checkRunPlan();
      } catch (e) {
        Alpine.store('app').toast(`Cannot resume run: ${e.message}`, 'error');
      }
    },

    clearOtherSessionJob(sid) {
      if (!this.activeJobSessionId || this.activeJobSessionId === sid) return;
      this.clearJobView();
    },

    clearJobView() {
      this._eventSource?.close();
      this._eventSource = null;
      this.activeJobId = null;
      this.activeJobType = null;
      this.activeJobSessionId = null;
      this.jobState = null;
      this.running = null;
      this.analyzeProgress = null;
      this.enrichProgress = null;
      this.dedupProgress = null;
      this.relateProgress = null;
      this.proposeProgress = null;
      this.organizeProgress = null;
      this.executeProgress = null;
    },

    async checkActiveJob() {
      const sid = Alpine.store('session').current_session_id;
      const requestId = ++this._activeJobRequest;
      this.clearOtherSessionJob(sid);
      try {
        const data = await api.get('/pipeline/jobs/active');
        if (requestId !== this._activeJobRequest || sid !== Alpine.store('session').current_session_id) return;
        if (data.job_id && data.session_id === sid) {
          this.activeJobId = data.job_id;
          this.activeJobType = data.job_type;
          this.activeJobSessionId = sid;
          this.jobState = data.state;
          this.running = data.job_type;
          this.progressStartTime = Date.now();
          if (data.progress) {
            // Route the in-flight progress to the right state var so the UI
            // re-renders with the existing snapshot while we reconnect SSE.
            this._setProgressForType(data.job_type, data.progress);
          }
          // Reconnect SSE stream
          this._connectJobStream(data.job_id, data.job_type);
        } else if (this.activeJobSessionId === sid) {
          this.clearJobView();
        }
      } catch (e) { /* ignore */ }
    },

    getSettings() {
      const session = Alpine.store('session');
      const analyzeModel = session.analyze_model || localStorage.getItem('datahoarder_analyze_model') || session.model || localStorage.getItem('datahoarder_model') || '';
      const proposeModel = session.propose_model || localStorage.getItem('datahoarder_propose_model') || session.model || localStorage.getItem('datahoarder_model') || '';
      return {
        rootPath: session.root_path || localStorage.getItem('datahoarder_folder') || '',
        analyzeModel,
        proposeModel,
        model: analyzeModel,  // backward compat
        backend: session.backend || localStorage.getItem('datahoarder_backend') || 'ollama',
        workers: session.workers || parseInt(localStorage.getItem('datahoarder_workers') || '1', 10),
        preferredLanguage: session.preferred_language || localStorage.getItem('datahoarder_preferred_language') || 'leave_as_is',
        session_id: session.current_session_id || '',
      };
    },

    isStepDone(step) {
      const steps = Alpine.store('session').stats?.completed_steps || [];
      return steps.includes(step);
    },

    formatSize(size) {
      if (size > 1024 * 1024) return (size / 1024 / 1024).toFixed(1) + ' MB';
      if (size > 1024) return (size / 1024).toFixed(0) + ' KB';
      return size + ' B';
    },

    formatBytes(size) {
      if (size >= 1024 ** 4) return (size / 1024 ** 4).toFixed(1) + ' TB';
      if (size >= 1024 ** 3) return (size / 1024 ** 3).toFixed(1) + ' GB';
      return this.formatSize(size);
    },

    escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
      );
    },

    renderTree(node, depth = 0) {
      // Renders both folders and individual files as flex rows. Long
      // filenames are ellipsis-truncated at the box boundary; the native
      // `title` tooltip shows the full name on hover. Loose files at the
      // root level (the 452 MB PDF, stray HTMLs) appear at depth 0 so the
      // user can see unorganized content at a glance.
      if (!node || typeof node !== 'object') return '';
      let html = '';
      const indentStr = '  '.repeat(depth);
      const indentHtml = indentStr
        ? `<span class="tree-indent">${indentStr}</span>`
        : '';

      // Folders (keys not starting with _) come first. Empty leaf folders
      // (0 files, 0 B, no child folders) get a dimmed style + "(empty)"
      // hint so browser-save cruft like "Tunic With Hood – Sewing Pattern
      // #4742..._files/" is visually obvious as a deletion candidate.
      for (const [key, val] of Object.entries(node)) {
        if (key.startsWith('_')) continue;
        if (!val || typeof val !== 'object') continue;
        const files = val._files || 0;
        const size = val._size || 0;
        const hasChildren = Object.keys(val).some(k => !k.startsWith('_'));
        const isEmpty = files === 0 && size === 0 && !hasChildren;
        const meta = isEmpty
          ? '(empty)'
          : `(${files} files, ${this.formatSize(size)})`;
        const titleAttr = this.escapeHtml(`${key}/  ${meta}`);
        const folderClass = isEmpty ? 'tree-folder tree-empty' : 'tree-folder';
        html +=
          `<div class="tree-line" title="${titleAttr}">` +
            indentHtml +
            `<span class="tree-name ${folderClass}">📁 ${this.escapeHtml(key)}/</span>` +
            `<span class="tree-meta">${this.escapeHtml(meta)}</span>` +
          `</div>`;
        html += this.renderTree(val, depth + 1);
      }

      // Individual files from _sample_files. At the root level these are
      // the loose files that need subfolder assignment — surfacing them is
      // half the point of the Organize step.
      const samples = Array.isArray(node._sample_files) ? node._sample_files : [];
      for (const entry of samples) {
        if (!entry || typeof entry !== 'object') continue;
        const name = entry.name || '';
        const size = entry.size || 0;
        const meta = `(${this.formatSize(size)})`;
        const titleAttr = this.escapeHtml(`${name}  ${meta}`);
        html +=
          `<div class="tree-line" title="${titleAttr}">` +
            indentHtml +
            `<span class="tree-name tree-file">📄 ${this.escapeHtml(name)}</span>` +
            `<span class="tree-meta">${this.escapeHtml(meta)}</span>` +
          `</div>`;
      }
      const truncated = node._sample_truncated || 0;
      if (truncated > 0) {
        const more = `… +${truncated} more file${truncated === 1 ? '' : 's'}`;
        html +=
          `<div class="tree-line">` +
            indentHtml +
            `<span class="tree-meta">${this.escapeHtml(more)}</span>` +
          `</div>`;
      }

      return html;
    },

    // Steps that run as background jobs (resilient to disconnects).
    // Everything except 'scan' and 'execute-commit' is a background job now.
    _BACKGROUND_STEPS: new Set([
      'enrich', 'dedup', 'relate', 'analyze', 'propose', 'organize', 'execute-dry',
    ]),

    async loadCommitPreview() {
      const sid = Alpine.store('session').current_session_id;
      if (!sid) {
        Alpine.store('app').toast('Select a session first', 'error');
        return;
      }
      try {
        this.commitPreview = await api.get(`/execute/preview?session_id=${encodeURIComponent(sid)}`);
        if (this.commitPreview.errors) {
          Alpine.store('app').toast(`${this.commitPreview.errors} reviewed action(s) cannot run; inspect the preview`, 'error');
        }
      } catch (e) {
        this.commitPreview = null;
        Alpine.store('app').toast(`Could not preview changes: ${e.message}`, 'error');
      }
    },

    async runStep(step) {
      // Prevent starting a new step while a job is active
      if (this.activeJobId && (this.jobState === 'running' || this.jobState === 'paused')) {
        Alpine.store('app').toast('A job is already running. Pause or wait for it to finish.', 'error');
        return;
      }
      const runRequestId = ++this._runStepRequestId;

      this.running = step;
      this.result = null;
      this.analyzeProgress = null;
      this.enrichProgress = null;
      this.dedupProgress = null;
      this.relateProgress = null;
      this.proposeProgress = null;
      this.organizeProgress = null;
      this.executeProgress = null;
      this.progressStartTime = null;
      this.activeJobId = null;
      this.activeJobType = null;
      this.jobState = null;
      Alpine.store('app').loading = true;
      const settings = this.getSettings();
      let foregroundOperation = null;
      try {
        let data;
        switch (step) {
          case 'scan':
            if (!settings.rootPath) {
              Alpine.store('app').toast('Select a folder in Setup first', 'error');
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            const skipDirs = Alpine.store('session').skip_dirs || [];
            const scanApp = Alpine.store('app');
            foregroundOperation = { id: ++scanApp.foregroundOperationSeq, session_id: settings.session_id, step };
            scanApp.foregroundOperation = foregroundOperation;
            data = await api.post('/pipeline/scan', { root_path: settings.rootPath, session_id: settings.session_id, skip_dirs: skipDirs });
            Alpine.store('app').toast(`Scan complete: ${data.new || 0} new files, ${data.skipped || 0} skipped`, 'success');
            break;
          case 'enrich':
          case 'dedup':
          case 'propose':
          case 'execute-dry':
            // Steps with no extra prerequisite checks
            data = await this._startBackgroundJob(step, settings);
            return;  // _connectJobStream handles the rest
          case 'relate':
            // Relate uses propose_model for reasoning
            if (!settings.proposeModel || settings.proposeModel === '') {
              Alpine.store('app').toast('Please select a proposal model in the Setup tab before relating', 'error');
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await this._startBackgroundJob(step, settings);
            return;
          case 'analyze':
            if (!settings.analyzeModel || settings.analyzeModel === '') {
              Alpine.store('app').toast('Please select an analysis model in the Setup tab before analyzing', 'error');
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await this._startBackgroundJob(step, settings);
            return;
          case 'organize':
            if (!settings.proposeModel || settings.proposeModel === '') {
              Alpine.store('app').toast('Please select a proposal model in the Setup tab before organizing', 'error');
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await this._startBackgroundJob(step, settings);
            return;
          case 'execute-commit':
            // Require a visible, session-scoped preview before the confirmation.
            if (!this.commitPreview || this.commitPreview.session_id !== settings.session_id) {
              Alpine.store('app').toast('Preview reviewed changes before committing', 'error');
              break;
            }
            if (!this.commitPreview.total) {
              Alpine.store('app').toast('No approved or edited proposals to apply', 'info');
              break;
            }
            if (this.commitPreview.errors) {
              Alpine.store('app').toast('Resolve the failing preview actions before committing', 'error');
              break;
            }
            if (!await window.appConfirm(
              `Apply ${this.commitPreview.total} reviewed change(s) to disk?\n\nThe preview below lists the selected files. If the proposals have changed, you will need to preview again.`,
              { title: 'Apply changes', confirmLabel: 'Apply changes', danger: true }
            )) {
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            const commitApp = Alpine.store('app');
            foregroundOperation = { id: ++commitApp.foregroundOperationSeq, session_id: settings.session_id, step };
            commitApp.foregroundOperation = foregroundOperation;
            data = await api.post('/execute', { session_id: settings.session_id, dry_run: false, preview_token: this.commitPreview.token });
            this.commitPreview = null;
            Alpine.store('app').toast('Changes applied to disk', 'success');
            break;
        }
        this.result = data;
      } catch (e) {
        if (step === 'execute-commit') this.commitPreview = null;
        Alpine.store('app').toast(`${step} failed: ${e.message}`, 'error');
        this.result = { error: e.message };
      } finally {
        if (foregroundOperation && Alpine.store('app').foregroundOperation?.id === foregroundOperation.id)
          Alpine.store('app').foregroundOperation = null;
        // Background jobs clean up on completion. A failed or stale launch
        // has no active job and must not leave this browser in a loading state.
        if (runRequestId === this._runStepRequestId &&
            (!this._BACKGROUND_STEPS.has(step) || !this.activeJobId)) {
          this.running = null;
          Alpine.store('app').loading = false;
          if (!this._BACKGROUND_STEPS.has(step)) await this._refreshAfterStep();
        }
      }
    },

    async _startBackgroundJob(type, settings) {
      // Start the background job via POST — returns immediately with job_id
      const body = { session_id: settings.session_id || '' };
      // Per-type request body extras
      if (type === 'analyze') {
        body.backend = settings.backend;
        body.model = settings.analyzeModel || settings.model;
        body.workers = settings.workers;
        body.sequence_sample_stride = Number(this.sequenceSampleStride) || 0;
        body.use_cache = this.useAnalysisCache;
      } else if (type === 'relate' || type === 'organize' || type === 'propose') {
        body.backend = settings.backend;
        body.model = settings.proposeModel || settings.model;
      } else if (type === 'execute-dry') {
        body.dry_run = true;
        body.min_confidence = settings.minConfidence ?? 0.7;
      }
      // Map step name to API endpoint path. Most steps use /pipeline/<type>,
      // but execute-dry is special: the existing endpoint is /execute and
      // the body's dry_run flag selects the background path.
      const url = (type === 'execute-dry') ? '/execute' : `/pipeline/${type}`;
      const res = await api.post(url, body);
      if (settings.session_id !== Alpine.store('session').current_session_id) return res;
      this.activeJobId = res.job_id;
      this.activeJobType = type;
      this.activeJobSessionId = settings.session_id;
      this.jobState = 'running';
      this.progressStartTime = Date.now();
      Alpine.store('app').loading = false;

      // Connect to the SSE stream for progress
      this._connectJobStream(res.job_id, type);
      return res;
    },

    _setProgressForType(type, data) {
      // Route progress dict to the right state var so UI bindings stay clean.
      switch (type) {
        case 'analyze':    this.analyzeProgress = data; break;
        case 'enrich':     this.enrichProgress = data; break;
        case 'dedup':      this.dedupProgress = data; break;
        case 'relate':     this.relateProgress = data; break;
        case 'propose':    this.proposeProgress = data; break;
        case 'organize':   this.organizeProgress = data; break;
        case 'execute-dry':
        case 'execute':    this.executeProgress = data; break;
      }
    },

    _connectJobStream(jobId, type) {
      const sid = this.activeJobSessionId;
      // Close any existing connection
      if (this._eventSource) {
        this._eventSource.close();
        this._eventSource = null;
      }

      const es = new EventSource(`/api/pipeline/jobs/${jobId}/stream`);
      this._eventSource = es;

      es.onmessage = (event) => {
        if (sid !== Alpine.store('session').current_session_id) return;
        try {
          const data = JSON.parse(event.data);

          // Skip pure heartbeats (no useful payload)
          if (data.heartbeat && data.done !== true && data.cancelled !== true && !data.state) return;

          // Update progress
          this._setProgressForType(type, data);

          // Update job state if provided
          if (data.state) this.jobState = data.state;

          // Handle completion
          if (data.done === true) {
            es.close();
            this._eventSource = null;
            this._onJobComplete(type, data);
          }
        } catch (e) { /* ignore parse errors */ }
      };

      es.onerror = () => {
        // EventSource will auto-reconnect; no action needed
        // But if the job is done, clean up
        if (this.jobState === 'completed' || this.jobState === 'failed' || this.jobState === 'cancelled') {
          es.close();
          this._eventSource = null;
        }
      };
    },

    async _onJobComplete(type, data) {
      if (this.activeJobSessionId !== Alpine.store('session').current_session_id) return;
      const state = data.state || 'completed';
      this.jobState = state;

      if (state === 'completed') {
        // Per-type success toast
        switch (type) {
          case 'analyze':
            Alpine.store('app').toast(
              `Analyze complete: ${data.analyzed || 0} fresh, ${data.cached || 0} cached, ${data.sampled || 0} sampled out, ${data.skipped || 0} skipped, ${data.errors || 0} errors`,
              'success',
            );
            break;
          case 'enrich':
            Alpine.store('app').toast(`Enrich complete: ${data.enriched || 0} files enriched`, 'success');
            break;
          case 'dedup': {
            const exact = data.exact?.groups || 0;
            const perc = data.perceptual?.groups || 0;
            const props = (data.proposals && (data.proposals.created ?? data.proposals.proposals)) || 0;
            Alpine.store('app').toast(
              `Dedup complete: ${exact} exact + ${perc} perceptual groups, ${props} proposals`,
              'success',
            );
            await Alpine.store('dedupCoverage').load();
            break;
          }
          case 'relate':
            Alpine.store('app').toast(
              `Relate complete: ${data.groups || 0} groups (${data.llm_groups || 0} LLM + ${data.backstop_groups || 0} backstop) across ${data.directories || 0} dirs`,
              'success',
            );
            break;
          case 'propose':
            Alpine.store('app').toast(`Propose complete: ${data.rename || 0} renames + ${data.tags || 0} tags`, 'success');
            break;
          case 'organize':
            Alpine.store('app').toast(`Organize complete: ${data.moves || data.move || 0} move proposals generated`, 'success');
            // Fetch the before/after trees on demand for the result panel
            try {
              const sid = Alpine.store('session').current_session_id;
              if (sid) {
                const trees = await api.get(`/pipeline/organize/trees/${sid}`);
                data.before_tree = trees.before_tree;
                data.after_tree = trees.after_tree;
              }
            } catch (_) { /* tree fetch is best-effort */ }
            await this.loadOrganizationCoverage();
            break;
          case 'execute-dry':
          case 'execute':
            Alpine.store('app').toast(
              `Dry run complete: ${data.applied || 0} would apply, ${data.failed || 0} failed, ${data.skipped || 0} skipped`,
              'success',
            );
            break;
        }
      } else if (state === 'failed') {
        Alpine.store('app').toast(`${type} failed: ${data.error || 'Unknown error'}`, 'error');
      } else if (state === 'cancelled') {
        Alpine.store('app').toast(`${type} cancelled`, 'info');
      }

      this.result = data;
      this.running = null;
      this.activeJobId = null;
      this.activeJobType = null;
      this.activeJobSessionId = null;
      // Reset all progress vars
      this.analyzeProgress = null;
      this.enrichProgress = null;
      this.dedupProgress = null;
      this.relateProgress = null;
      this.proposeProgress = null;
      this.organizeProgress = null;
      this.executeProgress = null;
      this.progressStartTime = null;

      await this._refreshAfterStep();
      if (type === 'analyze') await this.loadAnalysisErrors();
      if (['scan', 'analyze'].includes(type)) await this.loadAnalysisCoverage();

      await this.checkRunPlan();
    },

    async _refreshAfterStep() {
      // Refresh session state
      const sid = Alpine.store('session').current_session_id;
      if (sid) {
        try {
          const sessData = await api.get(`/sessions/${sid}`);
          Alpine.store('session').loadFrom(sessData);
        } catch (e) { /* ignore */ }
      }
      await refreshAllTabs();
    },

    async pauseJob() {
      if (!this.activeJobId) return;
      try {
        await api.post(`/pipeline/jobs/${this.activeJobId}/pause`);
        this.jobState = 'paused';
        Alpine.store('app').toast('Job paused', 'info');
      } catch (e) {
        Alpine.store('app').toast('Failed to pause: ' + e.message, 'error');
      }
    },

    async resumeJob() {
      if (!this.activeJobId) return;
      try {
        await api.post(`/pipeline/jobs/${this.activeJobId}/resume`);
        this.jobState = 'running';
        Alpine.store('app').toast('Job resumed', 'success');
      } catch (e) {
        Alpine.store('app').toast('Failed to resume: ' + e.message, 'error');
      }
    },

    async cancelJob() {
      if (!this.activeJobId) return;
      try {
        await api.post(`/pipeline/jobs/${this.activeJobId}/cancel`);
        Alpine.store('app').toast('Cancelling job...', 'info');
      } catch (e) {
        Alpine.store('app').toast('Failed to cancel: ' + e.message, 'error');
      }
    },

    filesPerMin(progress) {
      if (!this.progressStartTime || !progress || !progress.current) return null;
      const elapsed = (Date.now() - this.progressStartTime) / 1000;
      if (elapsed < 1) return null;
      return (progress.current / elapsed * 60).toFixed(1);
    },

    // ==========================================================================
    // UNATTENDED MODE
    // Runs the entire pipeline (scan → enrich → dedup → relate → analyze →
    // propose → organize → execute-dry) automatically, waiting for each step
    // to finish before starting the next. Does NOT auto-commit changes.
    // Use case: leave running overnight, review proposals in the morning.
    // ==========================================================================

    formatElapsed(ms) {
      const totalSec = Math.round(ms / 1000);
      const h = Math.floor(totalSec / 3600);
      const m = Math.floor((totalSec % 3600) / 60);
      const s = totalSec % 60;
      if (h > 0) return `${h}h ${m}m`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    },

    unattendedElapsed() {
      if (!this.unattendedStartTime) return '';
      return this.formatElapsed(Date.now() - this.unattendedStartTime);
    },

    async runUnattended() {
      if (this.unattendedMode) {
        const cancelRun = await window.appConfirm(
          'Cancel the unattended run? The current worker will stop at its next safe checkpoint.',
          { title: 'Cancel run', confirmLabel: 'Cancel run', danger: true }
        );
        if (!cancelRun) return;
        try {
          await api.post(`/pipeline/runs/${this.runPlanId}/cancel`, {
            session_id: Alpine.store('session').current_session_id,
          });
          await this.checkRunPlan();
          Alpine.store('app').toast('Cancelling the current worker...', 'info');
        } catch (e) {
          Alpine.store('app').toast(`Cannot cancel run: ${e.message}`, 'error');
        }
        return;
      }

      const settings = this.getSettings();
      if (!settings.session_id || !settings.rootPath) {
        Alpine.store('app').toast('Create a session and select its folder in Setup first', 'error');
        return;
      }
      if (!settings.analyzeModel || !settings.proposeModel) {
        Alpine.store('app').toast('Choose analysis and proposal models in Setup first', 'error');
        return;
      }
      if (this.running || this.activeJobId) {
        Alpine.store('app').toast('Wait for the current step to finish first', 'error');
        return;
      }
      const confirmMsg =
        'Start unattended run?\n\n' +
        'Will run: scan → enrich → analyze → dedup → relate → propose → organize → dry run\n\n' +
        'The server saves each completed step. No file changes will be committed.\n' +
        'Review proposals before applying any changes.';
      if (!await window.appConfirm(confirmMsg, { title: 'Unattended run', confirmLabel: 'Start' })) return;
      try {
        const response = await api.post('/pipeline/runs', {
          session_id: settings.session_id, root_path: settings.rootPath,
          backend: settings.backend, analyze_model: settings.analyzeModel,
          propose_model: settings.proposeModel, workers: settings.workers,
          relate_scope: Alpine.store('session').relate_scope || 'per_directory',
          skip_dirs: Alpine.store('session').skip_dirs || [],
          sequence_sample_stride: Number(this.sequenceSampleStride) || 0,
          use_cache: this.useAnalysisCache,
        });
        this.runPlanId = response.plan.plan_id;
        await this.checkRunPlan();
        Alpine.store('app').toast('Run saved and started. Progress survives a page reload.', 'success');
      } catch (e) {
        Alpine.store('app').toast(`Cannot start unattended run: ${e.message}`, 'error');
      }
    },
  }));

});
