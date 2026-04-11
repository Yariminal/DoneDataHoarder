/* ============================================================
   DataHoarder — Alpine.js frontend application
   ============================================================ */

document.addEventListener('alpine:init', () => {

  /* ----------------------------------------------------------
   * Global store
   * -------------------------------------------------------- */
  Alpine.store('app', {
    tab: 'home',
    toasts: [],
    loading: false,
    version: '0.3.0',

    toast(msg, type = 'info') {
      const id = Date.now();
      this.toasts.push({ id, msg, type });
    },
    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },
  });

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
    workers: 1,
    preferred_language: 'leave_as_is',
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
      this.workers = data.workers || 1;
      this.preferred_language = data.preferred_language || 'leave_as_is';
      this.stats = data.stats || {};
      this.file_count = data.file_count || 0;
      this.proposal_count = data.proposal_count || 0;
      this.duplicate_count = data.duplicate_count || 0;
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
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
      name = prompt('Enter a name for this session:', `Session ${new Date().toLocaleDateString()}`);
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

  window.goHome = function () {
    const session = Alpine.store('session');
    if (session.active && session.is_unsaved) {
      if (!confirm('You have unsaved changes. Leave without saving?')) return;
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
        });
        Alpine.store('session').loadFrom(data);
        // Clear localStorage so new session doesn't inherit old settings
        localStorage.removeItem('datahoarder_model');
        localStorage.removeItem('datahoarder_folder');
        localStorage.removeItem('datahoarder_workers');
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
        if (data.backend) localStorage.setItem('datahoarder_backend', data.backend);
        if (data.workers) localStorage.setItem('datahoarder_workers', String(data.workers));
        if (data.preferred_language) localStorage.setItem('datahoarder_preferred_language', data.preferred_language);
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
      if (!confirm(`Permanently delete "${name}"? This cannot be undone.`)) return;
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

    async initResults() {
      await this.loadResultsList();
    },

    async loadResultsList() {
      try {
        this.savedResults = await api.get('/results/list');
      } catch (e) {
        console.error('Failed to load results list:', e);
      }
    },

    async saveResults(type) {
      const name = prompt(`Save ${type} results as:`, `${type}_${new Date().toISOString().slice(0, 10)}`);
      if (!name) return;

      try {
        const result = await api.post(`/results/save/${type}?name=${encodeURIComponent(name)}`);
        Alpine.store('app').toast(`Saved ${result.filename} (${result.message})`, 'success');
        await this.loadResultsList();
        this.currentResult = '';
      } catch (e) {
        Alpine.store('app').toast(`Failed to save results: ${e.message}`, 'error');
      }
    },

    async loadResult() {
      if (!this.currentResult) return;

      try {
        const result = await api.get(`/results/load/${encodeURIComponent(this.currentResult)}`);
        const data = result.data;

        if (data.items) {
          // Only set properties that exist on this component
          if (this.files !== undefined) this.files = data.items;
          if (this.proposals !== undefined) this.proposals = data.items;
          if (this.groups !== undefined) this.groups = data.items;
          this.total = data.total;
          this.page = 1;
        }

        Alpine.store('app').toast(`Loaded ${this.currentResult}`, 'success');
      } catch (e) {
        Alpine.store('app').toast(`Failed to load results: ${e.message}`, 'error');
      }
    },
  };

  /* ----------------------------------------------------------
   * Dashboard component
   * -------------------------------------------------------- */
  Alpine.data('dashboard', () => ({
    stats: null,
    _loadedVersion: -1,
    async init() {
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'dashboard' && this._loadedVersion < window._dataVersion) this.load();
      });
    },
    async load() {
      try {
        this.stats = await api.get('/stats');
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        Alpine.store('app').toast('Failed to load stats', 'error');
      }
    },
    formatBytes(b) {
      if (!b) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      const i = Math.floor(Math.log(b) / Math.log(1024));
      return (b / Math.pow(1024, i)).toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
    },
    pipelineProgress() {
      if (!this.stats) return [];
      const s = this.stats.by_status;
      const total = this.stats.total_files || 1;
      return [
        { label: 'Pending',  count: s.pending  || 0, pct: ((s.pending  || 0) / total * 100), cls: 'fill-warning' },
        { label: 'Enriched', count: s.enriched || 0, pct: ((s.enriched || 0) / total * 100), cls: 'fill-primary' },
        { label: 'Analyzed', count: s.analyzed || 0, pct: ((s.analyzed || 0) / total * 100), cls: 'fill-primary' },
        { label: 'Proposed', count: s.proposed || 0, pct: ((s.proposed || 0) / total * 100), cls: 'fill-primary' },
        { label: 'Applied',  count: s.applied  || 0, pct: ((s.applied  || 0) / total * 100), cls: 'fill-success' },
        { label: 'Skipped',  count: s.skipped  || 0, pct: ((s.skipped  || 0) / total * 100), cls: '' },
        { label: 'Error',    count: s.error    || 0, pct: ((s.error    || 0) / total * 100), cls: 'fill-warning' },
      ].filter(x => x.count > 0);
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
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'files' && this._loadedVersion < window._dataVersion) this.load();
      });
    },

    async load() {
      try {
        let url = `/files?page=${this.page}&per_page=${this.perPage}`;
        if (this.statusFilter) url += `&status=${this.statusFilter}`;
        if (this.mimeFilter) url += `&mime_prefix=${this.mimeFilter}`;
        if (this.search) url += `&search=${encodeURIComponent(this.search)}`;
        const data = await api.get(url);
        this.files = data.items;
        this.total = data.total;
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        Alpine.store('app').toast('Failed to load files', 'error');
      }
    },

    totalPages() { return Math.ceil(this.total / this.perPage) || 1; },

    async viewFile(id) {
      try {
        this.selectedFile = await api.get(`/files/${id}`);
        this.showModal = true;
      } catch (e) {
        Alpine.store('app').toast('Failed to load file details', 'error');
      }
    },

    closeModal() { this.showModal = false; this.selectedFile = null; },

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

    async prevPage() { if (this.page > 1) { this.page--; await this.load(); } },
    async nextPage() { if (this.page < this.totalPages()) { this.page++; await this.load(); } },
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
    _loadedVersion: -1,
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'proposals' && this._loadedVersion < window._dataVersion) this.load();
      });
    },

    async load() {
      try {
        let url = `/proposals?page=${this.page}&per_page=${this.perPage}`;
        if (this.statusFilter) url += `&status=${this.statusFilter}`;
        if (this.typeFilter)   url += `&proposal_type=${this.typeFilter}`;
        if (this.minConfidence > 0) url += `&min_confidence=${this.minConfidence / 100}`;
        if (this.search) url += `&search=${encodeURIComponent(this.search)}`;
        const data = await api.get(url);
        this.proposals = data.items;
        this.total = data.total;
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        Alpine.store('app').toast('Failed to load proposals', 'error');
      }
    },

    async approve(id) {
      try {
        await api.post(`/proposals/${id}/approve`);
        this.proposals = this.proposals.map(p => p.id === id ? { ...p, status: 'approved' } : p);
        Alpine.store('app').toast('Approved', 'success');
      } catch (e) {
        Alpine.store('app').toast('Approve failed', 'error');
      }
    },

    async reject(id) {
      try {
        await api.post(`/proposals/${id}/reject`);
        this.proposals = this.proposals.map(p => p.id === id ? { ...p, status: 'rejected' } : p);
        Alpine.store('app').toast('Rejected', 'success');
      } catch (e) {
        Alpine.store('app').toast('Reject failed', 'error');
      }
    },

    editingId: null,
    editValue: '',

    startEdit(p) {
      this.editingId = p.id;
      this.editValue = p.proposed_value || '';
    },

    async saveEdit(id) {
      try {
        await api.post(`/proposals/${id}/edit`, { proposed_value: this.editValue });
        this.proposals = this.proposals.map(p =>
          p.id === id ? { ...p, proposed_value: this.editValue, status: 'modified' } : p
        );
        this.editingId = null;
        Alpine.store('app').toast('Updated', 'success');
      } catch (e) {
        Alpine.store('app').toast('Edit failed', 'error');
      }
    },

    cancelEdit() { this.editingId = null; },

    async bulkApprove() {
      try {
        const data = await api.post('/proposals/bulk-approve', {
          min_confidence: this.bulkConfidence / 100,
          proposal_type: this.typeFilter || null,
        });
        Alpine.store('app').toast(`Approved ${data.approved} proposals`, 'success');
        await this.load();
      } catch (e) {
        Alpine.store('app').toast('Bulk approve failed', 'error');
      }
    },

    totalPages() { return Math.ceil(this.total / this.perPage) || 1; },
    async prevPage() { if (this.page > 1) { this.page--; await this.load(); } },
    async nextPage() { if (this.page < this.totalPages()) { this.page++; await this.load(); } },

    confColor(c) {
      if (!c) return 'var(--text-dim)';
      if (c >= 0.8) return 'var(--success)';
      if (c >= 0.5) return 'var(--warning)';
      return 'var(--danger)';
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
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
      this.$watch(() => Alpine.store('app').tab, (tab) => {
        if (tab === 'duplicates' && this._loadedVersion < window._dataVersion) this.load();
      });
    },

    async load() {
      try {
        const sid = Alpine.store('session').current_session_id;
        const data = await api.get(`/duplicates?page=${this.page}&per_page=20&session_id=${sid}`);
        this.groups = data.items;
        this.total = data.total;
        this._loadedVersion = window._dataVersion;
      } catch (e) {
        Alpine.store('app').toast('Failed to load duplicates', 'error');
      }
    },

    async setKeeper(groupId, fileId) {
      try {
        await api.post(`/duplicates/${groupId}/keeper`, { keep_file_id: fileId });
        this.groups = this.groups.map(g => {
          if (g.id === groupId) {
            g.keep_file_id = fileId;
            g.files = g.files.map(f => ({ ...f, is_keeper: f.id === fileId }));
          }
          return g;
        });
        Alpine.store('app').toast('Keeper set', 'success');
      } catch (e) {
        Alpine.store('app').toast('Failed to set keeper', 'error');
      }
    },

    formatBytes(b) {
      if (!b) return '0 B';
      if (b > 1024*1024*1024) return (b/1024/1024/1024).toFixed(1) + ' GB';
      if (b > 1024*1024) return (b/1024/1024).toFixed(1) + ' MB';
      return (b/1024).toFixed(0) + ' KB';
    },

    isImage(f) { return f.mime_type && f.mime_type.startsWith('image/'); },
  }));

  /* ----------------------------------------------------------
   * Setup component — folder, model, backend, workers
   * -------------------------------------------------------- */
  Alpine.data('setup', () => ({
    selectedFolder: localStorage.getItem('datahoarder_folder') || '',
    selectedModel: localStorage.getItem('datahoarder_model') || '',
    selectedBackend: localStorage.getItem('datahoarder_backend') || 'ollama',
    selectedWorkers: parseInt(localStorage.getItem('datahoarder_workers') || '1', 10),
    numParallel: parseInt(localStorage.getItem('datahoarder_num_parallel') || '1', 10),
    preferredLanguage: localStorage.getItem('datahoarder_preferred_language') || 'leave_as_is',
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

    async init() {
      await this.loadOllamaStatus();
      await this.loadInstalledModels();
      this.$watch('showBrowser', (val) => {
        if (val && !this.currentPath) {
          this.browsePath('');
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
      Alpine.store('app').toast('Folder selected: ' + path, 'success');
    },

    selectCustomModel() {
      if (!this.customModel.trim()) {
        Alpine.store('app').toast('Enter a model name', 'error');
        return;
      }
      this.selectedModel = this.customModel.trim();
      this.customModel = '';
      this.showCustomModel = false;
      this.saveSettings();
      Alpine.store('app').toast('Custom model selected: ' + this.selectedModel, 'success');
    },

    saveSettings() {
      localStorage.setItem('datahoarder_folder', this.selectedFolder);
      localStorage.setItem('datahoarder_model', this.selectedModel);
      localStorage.setItem('datahoarder_backend', this.selectedBackend);
      localStorage.setItem('datahoarder_workers', String(this.selectedWorkers));
      localStorage.setItem('datahoarder_num_parallel', String(this.numParallel));
      localStorage.setItem('datahoarder_preferred_language', this.preferredLanguage);
      // Sync to session store
      const session = Alpine.store('session');
      if (session.active) {
        session.root_path = this.selectedFolder;
        session.model = this.selectedModel;
        session.backend = this.selectedBackend;
        session.workers = this.selectedWorkers;
        session.preferred_language = this.preferredLanguage;
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
      if (!confirm(`Are you sure you want to delete ${modelName}? This cannot be undone.`)) {
        return;
      }
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
    // Progress tracking for analyze and enrich (background jobs)
    analyzeProgress: null,
    enrichProgress: null,
    progressStartTime: null,
    // Background job state
    activeJobId: null,
    activeJobType: null,
    jobState: null,  // 'running', 'paused', 'completed', 'failed', 'cancelled'
    _eventSource: null,

    async init() {
      // Check for an active background job (reconnect after page refresh)
      await this.checkActiveJob();
    },

    async checkActiveJob() {
      try {
        const data = await api.get('/pipeline/jobs/active');
        if (data.job_id) {
          this.activeJobId = data.job_id;
          this.activeJobType = data.job_type;
          this.jobState = data.state;
          this.running = data.job_type;
          this.progressStartTime = Date.now();
          if (data.progress) {
            if (data.job_type === 'analyze') this.analyzeProgress = data.progress;
            else if (data.job_type === 'enrich') this.enrichProgress = data.progress;
          }
          // Reconnect SSE stream
          this._connectJobStream(data.job_id, data.job_type);
        }
      } catch (e) { /* ignore */ }
    },

    getSettings() {
      const session = Alpine.store('session');
      return {
        rootPath: session.root_path || localStorage.getItem('datahoarder_folder') || '',
        model: session.model || localStorage.getItem('datahoarder_model') || '',
        backend: session.backend || localStorage.getItem('datahoarder_backend') || 'ollama',
        workers: session.workers || parseInt(localStorage.getItem('datahoarder_workers') || '1', 10),
        preferredLanguage: session.preferred_language || localStorage.getItem('datahoarder_preferred_language') || 'leave_as_is',
        session_id: session.current_session_id || '',
      };
    },

    async runStep(step) {
      // Prevent starting a new step while a job is active
      if (this.activeJobId && (this.jobState === 'running' || this.jobState === 'paused')) {
        Alpine.store('app').toast('A job is already running. Pause or wait for it to finish.', 'error');
        return;
      }

      this.running = step;
      this.result = null;
      this.analyzeProgress = null;
      this.enrichProgress = null;
      this.progressStartTime = null;
      this.activeJobId = null;
      this.activeJobType = null;
      this.jobState = null;
      Alpine.store('app').loading = true;
      const settings = this.getSettings();
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
            data = await api.post('/pipeline/scan', { root_path: settings.rootPath, session_id: settings.session_id });
            Alpine.store('app').toast(`Scan complete: ${data.new || 0} new files, ${data.skipped || 0} skipped`, 'success');
            break;
          case 'enrich':
            data = await this._startBackgroundJob('enrich', settings);
            return;  // _connectJobStream handles the rest
          case 'dedup':
            data = await api.post('/pipeline/dedup', { session_id: settings.session_id });
            const exactGroups = data.exact?.groups || 0;
            const percGroups = data.perceptual?.groups || 0;
            Alpine.store('app').toast(`Dedup complete: ${exactGroups} exact + ${percGroups} perceptual groups`, 'success');
            break;
          case 'analyze':
            if (!settings.model || settings.model === '') {
              Alpine.store('app').toast('Please select a model in the Setup tab before analyzing', 'error');
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await this._startBackgroundJob('analyze', settings);
            return;  // _connectJobStream handles the rest
          case 'propose':
            data = await api.post('/pipeline/propose', { session_id: settings.session_id });
            Alpine.store('app').toast(`Propose complete: ${data.rename || 0} renames + ${data.tags || 0} tags`, 'success');
            break;
          case 'organize':
            if (!settings.model || settings.model === '') {
              Alpine.store('app').toast('Please select a model in the Setup tab before organizing', 'error');
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await api.post('/pipeline/organize', {
              session_id: settings.session_id,
              backend: settings.backend,
              model: settings.model,
            });
            Alpine.store('app').toast(`Organize complete: ${data.move || 0} move proposals generated`, 'success');
            break;
          case 'execute-dry':
            data = await api.post('/execute', { session_id: settings.session_id, dry_run: true });
            Alpine.store('app').toast('Dry run complete — review results below', 'success');
            break;
          case 'execute-commit':
            if (!confirm('Apply all approved changes to disk? This cannot be undone.')) {
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await api.post('/execute', { session_id: settings.session_id, dry_run: false });
            Alpine.store('app').toast('Changes applied to disk', 'success');
            break;
        }
        this.result = data;
      } catch (e) {
        Alpine.store('app').toast(`${step} failed: ${e.message}`, 'error');
        this.result = { error: e.message };
      } finally {
        if (step !== 'analyze' && step !== 'enrich') {
          this.running = null;
          Alpine.store('app').loading = false;
          await this._refreshAfterStep();
        }
      }
    },

    async _startBackgroundJob(type, settings) {
      // Start the background job via POST — returns immediately with job_id
      const body = { session_id: settings.session_id || '' };
      if (type === 'analyze') {
        body.backend = settings.backend;
        body.model = settings.model;
        body.workers = settings.workers;
      }
      const res = await api.post(`/pipeline/${type}`, body);
      this.activeJobId = res.job_id;
      this.activeJobType = type;
      this.jobState = 'running';
      this.progressStartTime = Date.now();
      Alpine.store('app').loading = false;

      // Connect to the SSE stream for progress
      this._connectJobStream(res.job_id, type);
      return res;
    },

    _connectJobStream(jobId, type) {
      // Close any existing connection
      if (this._eventSource) {
        this._eventSource.close();
        this._eventSource = null;
      }

      const es = new EventSource(`/api/pipeline/jobs/${jobId}/stream`);
      this._eventSource = es;

      es.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);

          // Skip heartbeats
          if (data.heartbeat) return;

          // Update progress
          if (type === 'analyze') this.analyzeProgress = data;
          else if (type === 'enrich') this.enrichProgress = data;

          // Update job state if provided
          if (data.state) this.jobState = data.state;

          // Handle completion
          if (data.done) {
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
      const state = data.state || 'completed';
      this.jobState = state;

      if (state === 'completed') {
        if (type === 'analyze') {
          Alpine.store('app').toast(
            `Analyze complete: ${data.analyzed || 0} analyzed, ${data.skipped || 0} skipped, ${data.errors || 0} errors`,
            'success'
          );
        } else if (type === 'enrich') {
          Alpine.store('app').toast(`Enrich complete: ${data.enriched || 0} files enriched`, 'success');
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
      this.analyzeProgress = null;
      this.enrichProgress = null;
      this.progressStartTime = null;

      await this._refreshAfterStep();
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
  }));

});
