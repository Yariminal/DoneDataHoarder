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

    toast(msg, type = 'info') {
      const id = Date.now();
      this.toasts.push({ id, msg, type });
    },
    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
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
   * Refresh helper — reloads all data tabs after pipeline ops
   * -------------------------------------------------------- */
  window.refreshAllTabs = async function () {
    // Dispatch a custom event that all components can listen for
    document.dispatchEvent(new CustomEvent('datahoarder:refresh'));
  };

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
    async init() {
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
    },
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
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
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
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
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
    ...resultsMixin,

    async init() {
      await this.initResults();
      await this.load();
      document.addEventListener('datahoarder:refresh', () => this.load());
    },

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
   * Setup component — folder, model, backend, workers
   * -------------------------------------------------------- */
  Alpine.data('setup', () => ({
    selectedFolder: localStorage.getItem('datahoarder_folder') || '',
    selectedModel: localStorage.getItem('datahoarder_model') || 'gemma3:12b',
    selectedBackend: localStorage.getItem('datahoarder_backend') || 'ollama',
    selectedWorkers: parseInt(localStorage.getItem('datahoarder_workers') || '1', 10),
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
        const res = await api.post('/ollama/start');
        Alpine.store('app').toast(res.status, 'success');
        await new Promise(r => setTimeout(r, 3000));
        await this.loadOllamaStatus();
      } catch (e) {
        Alpine.store('app').toast('Failed to start Ollama: ' + e.message, 'error');
      }
    },
  }));

  /* ----------------------------------------------------------
   * Pipeline runner — reads settings from localStorage (Setup tab)
   * -------------------------------------------------------- */
  Alpine.data('pipeline', () => ({
    running: null,
    result: null,
    // Analyze progress tracking
    analyzeProgress: null,  // { current, total, analyzed, skipped, errors }

    async init() {
      // Nothing to init — settings come from localStorage via Setup tab
    },

    getSettings() {
      return {
        rootPath: localStorage.getItem('datahoarder_folder') || '',
        model: localStorage.getItem('datahoarder_model') || 'gemma3:12b',
        backend: localStorage.getItem('datahoarder_backend') || 'ollama',
        workers: parseInt(localStorage.getItem('datahoarder_workers') || '1', 10),
      };
    },

    async runStep(step) {
      this.running = step;
      this.result = null;
      this.analyzeProgress = null;
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
            data = await api.post('/pipeline/scan', { root_path: settings.rootPath });
            Alpine.store('app').toast(`Scan complete: ${data.new || 0} new files, ${data.skipped || 0} skipped`, 'success');
            break;
          case 'enrich':
            data = await api.post('/pipeline/enrich');
            Alpine.store('app').toast(`Enrich complete: ${data.enriched || 0} files enriched`, 'success');
            break;
          case 'dedup':
            data = await api.post('/pipeline/dedup');
            const exactGroups = data.exact?.new_groups || 0;
            const percGroups = data.perceptual?.new_groups || 0;
            Alpine.store('app').toast(`Dedup complete: ${exactGroups} exact + ${percGroups} perceptual groups`, 'success');
            break;
          case 'analyze':
            data = await this.runAnalyzeWithProgress(settings);
            Alpine.store('app').toast(
              `Analyze complete: ${data.analyzed || 0} analyzed, ${data.skipped || 0} skipped, ${data.errors || 0} errors`,
              'success'
            );
            break;
          case 'propose':
            data = await api.post('/pipeline/propose');
            Alpine.store('app').toast(`Propose complete: ${data.rename || 0} renames + ${data.tags || 0} tags`, 'success');
            break;
          case 'execute-dry':
            data = await api.post('/execute?dry_run=true');
            Alpine.store('app').toast('Dry run complete — review results below', 'success');
            break;
          case 'execute-commit':
            if (!confirm('Apply all approved changes to disk? This cannot be undone.')) {
              this.running = null;
              Alpine.store('app').loading = false;
              return;
            }
            data = await api.post('/execute?dry_run=false');
            Alpine.store('app').toast('Changes applied to disk', 'success');
            break;
        }
        this.result = data;
      } catch (e) {
        Alpine.store('app').toast(`${step} failed: ${e.message}`, 'error');
        this.result = { error: e.message };
      } finally {
        this.running = null;
        this.analyzeProgress = null;
        Alpine.store('app').loading = false;
        // Refresh all data tabs
        await refreshAllTabs();
      }
    },

    async runAnalyzeWithProgress(settings) {
      // Use SSE endpoint for real-time progress
      const response = await fetch('/api/pipeline/analyze-stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          backend: settings.backend,
          model: settings.model,
          workers: settings.workers,
        }),
      });

      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finalResult = null;

      while (true) {
        const { done, value } = await reader.read();

        if (value) {
          buffer += decoder.decode(value, { stream: true });
        }

        if (done) {
          buffer += decoder.decode();
          // Process remaining buffer
          const lines = buffer.split('\n');
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.done) finalResult = data;
                else this.analyzeProgress = data;
              } catch (e) {}
            }
          }
          break;
        }

        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              if (data.done) {
                finalResult = data;
              } else if (data.error) {
                throw new Error(data.error);
              } else {
                this.analyzeProgress = data;
              }
            } catch (e) {
              if (e.message && !e.message.includes('JSON')) throw e;
            }
          }
        }
      }

      if (finalResult) {
        return finalResult;
      }
      throw new Error('Analyze stream ended without final result');
    },
  }));

});
