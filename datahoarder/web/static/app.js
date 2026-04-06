/* ============================================================
   DataHoarder — Alpine.js frontend application
   ============================================================ */

document.addEventListener('alpine:init', () => {

  /* ----------------------------------------------------------
   * Global store
   * -------------------------------------------------------- */
  Alpine.store('app', {
    tab: 'dashboard',
    toasts: [],
    loading: false,

    toast(msg, type = 'info', duration = 3500) {
      const id = Date.now();
      this.toasts.push({ id, msg, type });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, duration);
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
  };

  /* ----------------------------------------------------------
   * Dashboard component
   * -------------------------------------------------------- */
  Alpine.data('dashboard', () => ({
    stats: null,
    async init() { await this.load(); },
    async load() {
      try {
        this.stats = await api.get('/stats');
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
   * Files browser
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

    async init() { await this.load(); },

    async load() {
      try {
        let url = `/files?page=${this.page}&per_page=${this.perPage}`;
        if (this.statusFilter) url += `&status=${this.statusFilter}`;
        if (this.mimeFilter) url += `&mime_prefix=${this.mimeFilter}`;
        if (this.search) url += `&search=${encodeURIComponent(this.search)}`;
        const data = await api.get(url);
        this.files = data.items;
        this.total = data.total;
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
   * Proposals review
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

    async init() { await this.load(); },

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
   * Duplicates
   * -------------------------------------------------------- */
  Alpine.data('duplicates', () => ({
    groups: [],
    total: 0,
    page: 1,

    async init() { await this.load(); },

    async load() {
      try {
        const data = await api.get(`/duplicates?page=${this.page}&per_page=20`);
        this.groups = data.items;
        this.total = data.total;
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
   * Pipeline runner
   * -------------------------------------------------------- */
  /* ----------------------------------------------------------
   * Setup component
   * -------------------------------------------------------- */
  Alpine.data('setup', () => ({
    selectedFolder: localStorage.getItem('datahoarder_folder') || '',
    selectedModel: localStorage.getItem('datahoarder_model') || 'gemma3:12b',
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
    pullProgress: {},  // Track progress per model

    async init() {
      await this.loadOllamaStatus();
      await this.loadInstalledModels();
      // Watch for modal open to auto-load drives
      this.$watch('showBrowser', (val) => {
        if (val && !this.currentPath) {
          this.browsePath('');
        }
      });
      this.recommendedModels = [
        // Gemma 4 (Latest from Google - NOW AVAILABLE on Ollama!)
        { name: 'gemma4:31b',  desc: 'Gemma 4 31B - Highest quality, dense, multimodal, 256K context', size: '20 GB', vision: true, latest: true },
        { name: 'gemma4:26b',  desc: 'Gemma 4 26B - Mixture of Experts, balanced, multimodal, 256K context', size: '18 GB', vision: true, latest: true },
        { name: 'gemma4:e4b',  desc: 'Gemma 4 E4B - Edge variant, multimodal+audio, 128K context', size: '9.6 GB', vision: true, latest: true },
        { name: 'gemma4:e2b',  desc: 'Gemma 4 E2B - Lightweight edge, multimodal+audio, 128K context', size: '7.2 GB', vision: true, latest: true },
        // Gemma 2 (stable, proven quality)
        { name: 'gemma2:27b',  desc: 'Gemma 2 27B - High quality, multimodal, needs 20GB+ RAM', size: '16 GB', vision: true },
        { name: 'gemma2:9b',   desc: 'Gemma 2 9B - Best balance of quality/speed', size: '5.5 GB', vision: true },
        // Gemma 3 (solid performers)
        { name: 'gemma3:12b',  desc: 'Gemma 3 12B - Good quality, multimodal', size: '8.1 GB', vision: true },
        { name: 'gemma3:4b',   desc: 'Gemma 3 4B - Fast, lightweight, multimodal', size: '3.3 GB', vision: true },
        // Vision specialists
        { name: 'llava:13b',   desc: 'LLaVA 13B - Specialized vision model', size: '8.0 GB', vision: true },
        { name: 'llava:7b',    desc: 'LLaVA 7B - Lightweight vision', size: '4.7 GB', vision: true },
        // Lightweight text
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
      return this.installedModels.some(m => m.name === modelName);
    },

    async pullModel(modelName) {
      this.pulling = modelName;
      this.pullProgress[modelName] = 0;
      try {
        const response = await fetch(`/api/ollama/pull`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: modelName }),
        });

        if (!response.ok) {
          throw new Error(`${response.status} ${response.statusText}`);
        }

        // Handle Server-Sent Events for progress
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.progress !== undefined) {
                  this.pullProgress[modelName] = data.progress;
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

        Alpine.store('app').toast(`Downloaded ${modelName}`, 'success');
        await this.loadInstalledModels();
      } catch (e) {
        Alpine.store('app').toast(`Pull failed: ${e.message}`, 'error');
      } finally {
        this.pulling = null;
        delete this.pullProgress[modelName];
      }
    },

    async startOllama() {
      try {
        const res = await api.post('/ollama/start');
        Alpine.store('app').toast(res.status, 'success');
        await new Promise(r => setTimeout(r, 3000));
        await this.loadOllamaStatus();
      } catch (e) {
        Alpine.store('app').toast('Failed to start Ollama: ' + e.message, 'error');
      }
    },
  }));

  Alpine.data('pipeline', () => ({
    rootPath: localStorage.getItem('datahoarder_folder') || '',
    model: localStorage.getItem('datahoarder_model') || 'gemma3:12b',
    backend: 'ollama',
    workers: 1,
    running: null,
    result: null,

    async init() {
      // Reload settings from storage each time user visits this tab
      this.rootPath = localStorage.getItem('datahoarder_folder') || '';
      this.model = localStorage.getItem('datahoarder_model') || 'gemma3:12b';
    },

    async runStep(step) {
      this.running = step;
      this.result = null;
      Alpine.store('app').loading = true;
      try {
        let data;
        switch (step) {
          case 'scan':
            if (!this.rootPath) { Alpine.store('app').toast('Select a folder in Setup first', 'error'); this.running = null; break; }
            data = await api.post('/pipeline/scan', { root_path: this.rootPath });
            break;
          case 'enrich':
            data = await api.post('/pipeline/enrich');
            break;
          case 'dedup':
            data = await api.post('/pipeline/dedup');
            break;
          case 'analyze':
            data = await api.post('/pipeline/analyze', {
              backend: this.backend, model: this.model, workers: this.workers,
            });
            break;
          case 'propose':
            data = await api.post('/pipeline/propose');
            break;
          case 'execute-dry':
            data = await api.post('/execute?dry_run=true');
            break;
          case 'execute-commit':
            if (!confirm('Apply all approved changes to disk? This cannot be undone.')) break;
            data = await api.post('/execute?dry_run=false');
            break;
        }
        this.result = data;
        Alpine.store('app').toast(`${step} complete`, 'success');
      } catch (e) {
        Alpine.store('app').toast(`${step} failed: ${e.message}`, 'error');
        this.result = { error: e.message };
      } finally {
        this.running = null;
        Alpine.store('app').loading = false;
      }
    },
  }));

});
